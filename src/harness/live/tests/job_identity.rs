use std::rc::Rc;

use live::runtime::state_store::RuntimeStateStore;
use num::Decimal18;
use serde_json::json;

use harness_trading::fills::fees::aggregate_fees;
use harness_trading::fills::fees::AssetClass;
use harness_trading::fills::fees::FeeAssessment;
use harness_trading::fills::fees::FeeComponent;
use harness_trading::fills::fees::FeeContext;
use harness_trading::risk::kill_switch::KillSwitch;
use live::supervision::clock::ManualClock;
use live::supervision::configuration::SupervisorSettings;
use live::supervision::configuration::TraderConfig;
use live::supervision::feed::EmptyMarketFeed;
use live::supervision::feed::MarketFeed;
use live::supervision::feed::SupervisedFeed;
use live::supervision::journal::SupervisionJournal;
use live::supervision::models::SupervisionError;
use live::supervision::process::resolve_job_id;
use live::supervision::snapshots::SharedSnapshotStore;
use live::supervision::TraderSupervisor;

#[test]
fn resolve_job_id_fails_on_configured_persisted_mismatch() {
    let error = resolve_job_id(Some("job-a"), Some("job-b")).expect_err("mismatch");
    assert!(matches!(error, SupervisionError::Configuration(_)));
    assert_eq!(
        resolve_job_id(Some("job-a"), Some("job-a")).expect("match"),
        "job-a"
    );
    assert_eq!(
        resolve_job_id(Some("job-a"), None).expect("config"),
        "job-a"
    );
    assert_eq!(
        resolve_job_id(None, Some("job-b")).expect("persisted"),
        "job-b"
    );
}

#[test]
fn resolve_job_id_allocates_unique_opaque_identities() {
    let first = resolve_job_id(None, None).expect("first");
    let second = resolve_job_id(None, None).expect("second");
    assert_ne!(first, second);
    assert!(first.starts_with("job-"));
    assert_eq!(first.len(), 36);
    assert!(first[4..].chars().all(|ch| ch.is_ascii_hexdigit()));
}

#[test]
fn config_carries_job_id_and_deployment_metadata() {
    let value = json!({
        "mode": "paper",
        "seed": 7,
        "job_id": "job-config",
        "deployment": {"name": "rth"},
        "strategy_instances": [{"instance_id": "strategy-1"}],
        "risk_pipeline": {"on_reject": "reject_order", "rules": []},
    });
    let config = TraderConfig::from_value(&value).expect("config");
    assert_eq!(config.job_id.as_deref(), Some("job-config"));
    assert_eq!(config.deployment_name, "rth");

    let omission = json!({
        "mode": "paper",
        "seed": 7,
        "strategy_instances": [{"instance_id": "strategy-1"}],
    });
    let config = TraderConfig::from_value(&omission).expect("config");
    assert_eq!(config.job_id, None);
    assert_eq!(config.deployment_name, "trader");
}

fn trader_config(job_id: Option<&str>) -> TraderConfig {
    TraderConfig {
        job_id: job_id.map(str::to_string),
        deployment_name: "test".to_string(),
        mode: "paper".to_string(),
        seed: 1,
        strategy_instance_ids: vec!["strategy-1".to_string()],
        on_reject: "reject_order".to_string(),
        rule_ids: Vec::new(),
    }
}

fn supervisor(
    config: TraderConfig,
    state_job: &str,
    journal_job: &str,
) -> Result<TraderSupervisor, SupervisionError> {
    let shared = SharedSnapshotStore::in_memory();
    let mut state_store = RuntimeStateStore::new(state_job);
    state_store.add_store(Box::new(shared.clone()));
    let clock = Rc::new(ManualClock::new(0));
    let journal = SupervisionJournal::new(shared, journal_job, clock.clone());
    let feed = SupervisedFeed::new(
        Box::new(|| Ok(Box::new(EmptyMarketFeed) as Box<dyn MarketFeed>)),
        1,
        1_000_000,
        Rc::new(|_| {}),
    )
    .expect("feed");
    TraderSupervisor::new(
        config,
        SupervisorSettings::default(),
        None,
        state_store,
        journal,
        clock,
        feed,
        KillSwitch::new(None).expect("kill switch"),
        None,
        Box::new(|_| {}),
    )
}

#[test]
fn supervisor_requires_one_job_identity() {
    assert!(supervisor(trader_config(Some("job-a")), "job-b", "job-b").is_err());
    assert!(supervisor(trader_config(Some("job-a")), "job-a", "job-b").is_err());
    assert!(supervisor(trader_config(None), "job-a", "job-b").is_err());
    let adopted = supervisor(trader_config(None), "job-a", "job-a").expect("supervisor");
    assert_eq!(adopted.job_id(), "job-a");
    let pinned = supervisor(trader_config(Some("job-a")), "job-a", "job-a").expect("supervisor");
    assert_eq!(pinned.job_id(), "job-a");
}

#[test]
fn fee_aggregation_deduplicates_by_job_and_fill() {
    let component = FeeComponent::new("commission", "1.00".parse::<Decimal18>().expect("decimal"))
        .expect("component");
    let context =
        |job_id: &str| FeeContext::new(job_id, "strategy-1", AssetClass::Equity).expect("context");
    let assessment = |job_id: &str, fill_id: &str| {
        FeeAssessment::new(
            fill_id,
            "schedule-1",
            "USD",
            context(job_id),
            vec![component.clone()],
        )
        .expect("assessment")
    };

    assert_eq!(assessment("job-a", "fill-1").job_id(), "job-a");
    let across_jobs =
        aggregate_fees(&[assessment("job-a", "fill-1"), assessment("job-b", "fill-1")])
            .expect("distinct jobs");
    assert_eq!(across_jobs.by_job.len(), 2);

    let duplicate = aggregate_fees(&[assessment("job-a", "fill-1"), assessment("job-a", "fill-1")])
        .expect_err("duplicate");
    assert!(matches!(duplicate, live::LiveError::Fee(_)));
}
