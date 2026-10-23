//! Finite compute adapters and startup reservations. No engine or provider work.
use super::ExecutionStore;
use harness_execution::fleet::Slot;

/// One coalesced notification fills available slots. Unknown starts retain
/// their reservations; a hint is never retried as execution authority.
pub fn notify_pool() -> Result<(), String> {
    let store = super::configured_store().map_err(|e| e.to_string())?;
    let pending = store.queued().map_err(|e| e.to_string())?.len();
    if pending == 0 {
        return Ok(());
    }
    let max = std::env::var("HARNESS_EXECUTION_MAX_WORKERS")
        .ok()
        .map(|s| s.parse::<usize>().map_err(|e| e.to_string()))
        .transpose()?
        .unwrap_or(2);
    let azure = if std::env::var("HARNESS_MARKET_DATA_MODE").as_deref() == Ok("azure") {
        Some(AzureStart::prepare()?)
    } else {
        None
    };
    for _ in 0..pending.min(max) {
        let Some(ticket) = Slot::reserve(&store, max).map_err(|e| e.to_string())? else {
            break;
        };
        let result = if let Some(azure) = &azure {
            azure.start(&ticket)
        } else {
            local_start(&store, &ticket)
        };
        if let Err((rejected, error)) = result {
            if rejected {
                Slot::release_unstarted(&store, &ticket).map_err(|e| e.to_string())?;
            }
            return Err(error);
        }
    }
    Ok(())
}
fn local_start(store: &ExecutionStore, ticket: &str) -> Result<(), (bool, String)> {
    let start = (|| -> Result<(), String> {
        let binary = std::env::var_os("HARNESS_WORKER_EXECUTABLE")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| {
                std::env::current_exe()
                    .expect("process executable")
                    .with_file_name(if cfg!(windows) {
                        "harness-worker.exe"
                    } else {
                        "harness-worker"
                    })
            });
        let logs = crate::http::state::results_root_from_env().join("worker-logs");
        std::fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
        let log =
            std::fs::File::create(logs.join(format!("{ticket}.log"))).map_err(|e| e.to_string())?;
        let mut command = std::process::Command::new(binary);
        command
            .env("HARNESS_EXECUTION_SLOT_TOKEN", ticket)
            .env(
                "CARGO_BUILD_JOBS",
                std::env::var("CARGO_BUILD_JOBS").unwrap_or_else(|_| "2".into()),
            )
            .stdin(std::process::Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000);
        }
        let mut child = command.spawn().map_err(|e| e.to_string())?;
        let store = store.clone();
        let ticket = ticket.to_string();
        std::thread::spawn(move || {
            let _ = child.wait();
            let _ = Slot::release_unstarted(&store, &ticket);
        });
        Ok(())
    })();
    start.map_err(|error| (true, error))
}

struct AzureStart {
    agent: ureq::Agent,
    authorization: String,
    resource: String,
    template: serde_json::Value,
}
impl AzureStart {
    fn prepare() -> Result<Self, String> {
        use azure_core::auth::TokenCredential;
        let resource = std::env::var("HARNESS_EXECUTION_JOB_RESOURCE_ID")
            .map_err(|_| "HARNESS_EXECUTION_JOB_RESOURCE_ID is required")?;
        if !resource.starts_with("/subscriptions/")
            || !resource.contains("/providers/Microsoft.App/jobs/")
        {
            return Err("invalid execution job resource ID".into());
        }
        let runtime = tokio::runtime::Runtime::new().map_err(|e| e.to_string())?;
        let credential = azure_identity::DefaultAzureCredentialBuilder::new()
            .build()
            .map_err(|e| e.to_string())?;
        let token = runtime
            .block_on(credential.get_token(&["https://management.azure.com/.default"]))
            .map_err(|e| e.to_string())?;
        let authorization = format!("Bearer {}", token.token.secret());
        let agent = ureq::AgentBuilder::new()
            .redirects(0)
            .timeout(std::time::Duration::from_secs(30))
            .build();
        let job: serde_json::Value = agent
            .get(&format!(
                "https://management.azure.com{resource}?api-version=2025-07-01"
            ))
            .set("Authorization", &authorization)
            .call()
            .map_err(|e| e.to_string())?
            .into_json()
            .map_err(|e| e.to_string())?;
        let expected = std::env::var("HARNESS_EXECUTION_IMAGE")
            .map_err(|_| "HARNESS_EXECUTION_IMAGE is required")?;
        if !expected.contains("@sha256:") {
            return Err("execution image must be digest-pinned".into());
        }
        let template = start_template(
            job.pointer("/properties/template")
                .ok_or("execution Job has no template")?,
        )?;
        let containers = template["containers"]
            .as_array()
            .ok_or("execution Job has no containers")?;
        if containers.len() != 1
            || containers[0]["image"].as_str() != Some(&expected)
            || !containers[0]["args"].as_array().is_some_and(|args| {
                args.iter().any(|a| {
                    a.as_str()
                        .is_some_and(|a| a.ends_with("exec harness-worker"))
                })
            })
        {
            return Err(
                "execution Job template does not match the deployed worker contract".into(),
            );
        }
        Ok(Self {
            agent,
            authorization,
            resource,
            template,
        })
    }
    fn start(&self, ticket: &str) -> Result<(), (bool, String)> {
        let mut template = self.template.clone();
        template["containers"][0]["env"]
            .as_array_mut()
            .ok_or((true, "worker environment missing".into()))?
            .push(serde_json::json!({"name":"HARNESS_EXECUTION_SLOT_TOKEN","value":ticket}));
        let response=self.agent.post(&format!("https://management.azure.com{}/start?api-version=2025-07-01",self.resource)).set("Authorization",&self.authorization).send_json(template).map_err(|e|{
            let rejected=matches!(&e,ureq::Error::Status(code,_) if matches!(*code,400|401|403|404|409|422));(rejected,e.to_string())
        })?;
        let value: serde_json::Value = response.into_json().map_err(|e| (false, e.to_string()))?;
        let id = acknowledged_execution(&self.resource, &value).map_err(|error| (false, error))?;
        // Azure may return only an execution name. Verify the exact execution
        // through GET; neither a minimal nor an ambiguous ACK permits a second POST.
        let execution: serde_json::Value = self
            .agent
            .get(&format!(
                "https://management.azure.com{id}?api-version=2025-07-01"
            ))
            .set("Authorization", &self.authorization)
            .call()
            .map_err(|error| (false, error.to_string()))?
            .into_json()
            .map_err(|error| (false, error.to_string()))?;
        let container = &execution["properties"]["template"]["containers"][0];
        if container["image"] != self.template["containers"][0]["image"]
            || !container["env"].as_array().is_some_and(|rows| {
                rows.iter().any(|row| {
                    row["name"] == "HARNESS_EXECUTION_SLOT_TOKEN"
                        && row["value"].as_str() == Some(ticket)
                })
            })
        {
            return Err((
                false,
                "worker acknowledgement has mismatched image or startup ownership; reservation retained".into(),
            ));
        }
        Ok(())
    }
}

// Job definitions include volumes, probes and other fields which are not part
// of StartJobExecutionTemplate. Project the documented execution wire contract.
fn start_template(definition: &serde_json::Value) -> Result<serde_json::Value, String> {
    let containers = definition["containers"]
        .as_array()
        .ok_or("execution Job has no containers")?;
    if definition["initContainers"]
        .as_array()
        .is_some_and(|rows| !rows.is_empty())
        || definition["volumes"]
            .as_array()
            .is_some_and(|rows| !rows.is_empty())
    {
        return Err("execution workers must not require volumes or init containers".into());
    }
    let containers: Vec<_> = containers
        .iter()
        .map(|container| {
            let mut projected = serde_json::Map::new();
            for field in ["name", "image", "command", "args", "env", "resources"] {
                if let Some(value) = container.get(field).filter(|v| !v.is_null()) {
                    projected.insert(field.into(), value.clone());
                }
            }
            serde_json::Value::Object(projected)
        })
        .collect();
    Ok(serde_json::json!({"containers":containers}))
}

fn acknowledged_execution(resource: &str, value: &serde_json::Value) -> Result<String, String> {
    let prefix = format!("{resource}/executions/");
    let id = value["id"].as_str();
    let name = value["name"].as_str();
    let safe = |name: &str| {
        !name.is_empty() && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    };
    let Some(candidate) = id.map(str::to_owned).or_else(|| {
        name.filter(|name| safe(name))
            .map(|name| format!("{prefix}{name}"))
    }) else {
        return Err(
            "worker start acknowledgement has no valid execution identity; reservation retained"
                .into(),
        );
    };
    let suffix = candidate.get(prefix.len()..).filter(|suffix| safe(suffix));
    if !candidate
        .to_ascii_lowercase()
        .starts_with(&prefix.to_ascii_lowercase())
        || suffix.is_none()
        || name.is_some_and(|name| !suffix.unwrap().eq_ignore_ascii_case(name))
    {
        return Err(
            "worker start acknowledgement names another execution; reservation retained".into(),
        );
    }
    Ok(candidate)
}

#[cfg(test)]
#[path = "../../tests/unit/execution_launch.rs"]
mod tests;
