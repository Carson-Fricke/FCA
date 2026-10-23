//! Owns trader supervision process; lifecycle coordination remains in the parent.
use std::path::PathBuf;
use std::rc::Rc;

use crate::runtime::state_store::RuntimeStateStore;
use serde_json::json;
use serde_json::Value;

use super::clock::*;
use super::configuration::*;
use super::feed::*;
use super::journal::*;
use super::models::*;
use super::snapshots::*;
use super::TraderSupervisor;
use harness_trading::risk::kill_switch::KillSwitch;

/// Default supervision run document suffix.
pub const DEFAULT_CONFIG_PATH: &str = "config/harness/trader.yaml";

/// The environment variable pinning the trader execution job identifier.
pub const TRADER_JOB_ID_ENV: &str = "HARNESS_TRADER_JOB_ID";

pub(super) fn default_state_base() -> PathBuf {
    PathBuf::from(".local").join("trader-state")
}

pub(super) fn deployment_state_root(base: &std::path::Path, deployment_name: &str) -> PathBuf {
    base.join(deployment_name)
}

pub(super) fn job_identifier(value: &str, field: &str) -> Result<String, SupervisionError> {
    let text = value.trim();
    let valid = text
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric())
        && text
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'));
    if !valid {
        return Err(SupervisionError::Configuration(format!(
            "{field} contains invalid characters"
        )));
    }
    Ok(text.to_string())
}

pub(super) fn allocate_job_id() -> String {
    format!("job-{}", uuid::Uuid::new_v4().simple())
}

/// Resolve the one execution job identifier for a supervised process.
///
/// # Errors
///
/// Returns [`SupervisionError::Configuration`] when the configured identifier
/// conflicts with the persisted execution identity.
pub fn resolve_job_id(
    configured: Option<&str>,
    persisted: Option<&str>,
) -> Result<String, SupervisionError> {
    match (configured, persisted) {
        (Some(configured), Some(persisted)) if configured != persisted => {
            Err(SupervisionError::Configuration(format!(
                "configured job {configured:?} does not match persisted job {persisted:?}"
            )))
        }
        (Some(configured), _) => Ok(configured.to_string()),
        (None, Some(persisted)) => Ok(persisted.to_string()),
        (None, None) => Ok(allocate_job_id()),
    }
}

pub(super) fn requested_job_id(
    configured: Option<&str>,
) -> Result<Option<String>, SupervisionError> {
    let pinned = std::env::var(TRADER_JOB_ID_ENV)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    match (configured, pinned.as_deref()) {
        (Some(configured), Some(pinned)) if configured != pinned => {
            Err(SupervisionError::Configuration(format!(
                "configured job {configured:?} does not match {TRADER_JOB_ID_ENV} {pinned:?}"
            )))
        }
        (Some(configured), _) => Ok(Some(job_identifier(configured, "job_id")?)),
        (None, Some(pinned)) => Ok(Some(job_identifier(pinned, TRADER_JOB_ID_ENV)?)),
        (None, None) => Ok(None),
    }
}

pub(super) fn read_persisted_job_id(
    root: &std::path::Path,
) -> Result<Option<String>, SupervisionError> {
    let path = root.join("current.json");
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(SupervisionError::Configuration(format!(
                "persisted job identity at {} could not be read: {error}",
                path.display()
            )))
        }
    };
    let document: Value = serde_json::from_str(&text).map_err(|error| {
        SupervisionError::Configuration(format!(
            "persisted job identity at {} is not valid JSON: {error}",
            path.display()
        ))
    })?;
    let job_id = document
        .get("job_id")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            SupervisionError::Configuration(format!(
                "persisted job identity at {} does not carry job_id",
                path.display()
            ))
        })?;
    Ok(Some(job_identifier(job_id, "persisted job_id")?))
}

pub(super) fn persist_job_id(root: &std::path::Path, job_id: &str) -> Result<(), SupervisionError> {
    std::fs::create_dir_all(root).map_err(|error| {
        SupervisionError::Configuration(format!(
            "job state root {} could not be created: {error}",
            root.display()
        ))
    })?;
    let document = json!({"format_version": 1, "job_id": job_id});
    let text = serde_json::to_string(&document).map_err(|error| {
        SupervisionError::Configuration(format!("job identity could not be encoded: {error}"))
    })?;
    let path = root.join("current.json");
    let temporary = root.join("current.json.tmp");
    std::fs::write(&temporary, text).map_err(|error| {
        SupervisionError::Configuration(format!(
            "job identity at {} could not be persisted: {error}",
            temporary.display()
        ))
    })?;
    std::fs::rename(&temporary, &path).map_err(|error| {
        SupervisionError::Configuration(format!(
            "job identity at {} could not be persisted: {error}",
            path.display()
        ))
    })
}

pub(super) fn resolve_process_job_id(
    deployment_name: &str,
    configured: Option<&str>,
) -> Result<String, SupervisionError> {
    let requested = requested_job_id(configured)?;
    let root = deployment_state_root(&default_state_base(), deployment_name);
    let persisted = read_persisted_job_id(&root)?;
    let job_id = resolve_job_id(requested.as_deref(), persisted.as_deref())?;
    if persisted.as_deref() != Some(job_id.as_str()) {
        persist_job_id(&root, &job_id)?;
    }
    Ok(job_id)
}

/// Compose and run one supervised trader process from a configuration path.
///
/// This is the deployment composition used by both process entrypoints: it
/// resolves the execution job identifier, binds the durable local state store
/// and supervision journal, and runs one supervised paper or shadow session.
/// The market-data boundary is the deterministic in-process feed; the live
/// adapter is supplied by the runtime environment.
#[must_use]
pub fn run_trader_process(config_path: &std::path::Path) -> TraderExitCode {
    let mut configuration = match TraderConfig::load(config_path) {
        Ok(configuration) => configuration,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Usage;
        }
    };
    let job_id = match resolve_process_job_id(
        &configuration.deployment_name,
        configuration.job_id.as_deref(),
    ) {
        Ok(job_id) => job_id,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Usage;
        }
    };
    configuration.job_id = Some(job_id.clone());
    let clock = Rc::new(SystemClock);
    let shared = SharedSnapshotStore::new(Box::new(LocalFileSnapshotStore::new(
        deployment_state_root(&default_state_base(), &configuration.deployment_name),
    )));
    let mut state_store = RuntimeStateStore::new(&job_id);
    state_store.add_store(Box::new(shared.clone()));
    let journal = SupervisionJournal::new(shared, &job_id, clock.clone());
    let settings = SupervisorSettings::default();
    let now_ns = clock.now_ns();
    let session = Some(SessionWindow {
        open_ns: now_ns,
        close_ns: now_ns + DEFAULT_SESSION_NS,
    });
    let feed_factory: FeedFactory =
        Box::new(|| Ok(Box::new(EmptyMarketFeed) as Box<dyn MarketFeed>));
    let sleeper: Rc<dyn Fn(i64)> = Rc::new(|nanos: i64| {
        if nanos > 0 {
            std::thread::sleep(std::time::Duration::from_nanos(nanos as u64));
        }
    });
    let supervised_feed = match SupervisedFeed::new(
        feed_factory,
        settings.connect_attempts,
        settings.connect_backoff_ns,
        sleeper,
    ) {
        Ok(feed) => feed,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Failed;
        }
    };
    let kill_switch = match KillSwitch::new(None) {
        Ok(switch) => switch,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Failed;
        }
    };
    let mut supervisor = match TraderSupervisor::new(
        configuration,
        settings,
        session,
        state_store,
        journal,
        clock,
        supervised_feed,
        kill_switch,
        None,
        Box::new(|line| println!("{line}")),
    ) {
        Ok(supervisor) => supervisor,
        Err(error) => {
            eprintln!("{error}");
            return TraderExitCode::Usage;
        }
    };
    let outcome = supervisor.run();
    if let Some(detail) = &outcome.detail {
        eprintln!("{detail}");
    }
    outcome.exit_code
}

/// Read the latest supervision document for a configured execution job.
#[must_use]
pub fn read_supervision_status(config_path: &std::path::Path) -> Option<String> {
    let configuration = TraderConfig::load(config_path).ok()?;
    let root = deployment_state_root(&default_state_base(), &configuration.deployment_name);
    let job_id = match requested_job_id(configuration.job_id.as_deref()).ok()? {
        Some(job_id) => job_id,
        None => read_persisted_job_id(&root).ok()??,
    };
    let shared = SharedSnapshotStore::new(Box::new(LocalFileSnapshotStore::new(root)));
    shared.read_text(&format!("runs/{job_id}/supervision.json"))
}

use super::DEFAULT_SESSION_NS;
