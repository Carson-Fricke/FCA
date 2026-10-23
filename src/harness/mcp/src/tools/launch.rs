//! Owns MCP launch behavior over the injected capabilities; no host composition.
use super::*;
pub(super) struct LaunchTools {
    pub(super) mode: Boundary,
    pub(super) backtest_submitter: Option<Arc<dyn BacktestSubmitter>>,
    pub(super) study_submitter: Option<Arc<dyn StudySubmitter>>,
    pub(super) screening_submitter: Option<Arc<dyn ScreeningSubmitter>>,
    pub(super) runner: Option<Arc<dyn ProcessRunner>>,
    pub(super) program: Vec<String>,
    pub(super) configuration_loader: Arc<dyn ConfigurationLoader>,
    pub(super) remote: Arc<transport::RemoteClient>,
}
impl LaunchTools {
    /// Run one backtest through the selected local or remote boundary.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the boundary fails.
    pub(super) fn run_backtest(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = optional_identifier_argument(arguments, "job_id")?;
        let config_path = resolve_config(arguments, job_id.as_deref())?;
        if self.mode == Boundary::Remote {
            return self.launch_remote_backtest(&config_path, job_id.as_deref());
        }
        if let Some(submitter) = &self.backtest_submitter {
            let workspace = match arguments.get("workspace") {
                None | Some(Value::Null) => None,
                Some(Value::String(workspace)) if !workspace.trim().is_empty() => {
                    Some(workspace.clone())
                }
                Some(_) => {
                    return Err(ToolExecutionError::new(
                        "workspace must be a non-empty string",
                    ))
                }
            };
            return submitter
                .submit(&config_path, workspace.as_deref(), job_id.as_deref())
                .map_err(|error| {
                    ToolExecutionError::new(format!(
                        "the local backtest runner could not complete the backtest: {error}"
                    ))
                });
        }
        Err(ToolExecutionError::new(
            "local run_backtest requires its durable submission capability",
        ))
    }

    /// Run one study through the selected local or remote boundary.
    ///
    /// A study is a deterministic replay with no order execution: it fits and
    /// observes a research model and resolves indicators, publishing metrics
    /// without ever submitting orders. It is recorded as a study, never as a
    /// backtest.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the boundary fails.
    pub(super) fn run_study(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = optional_identifier_argument(arguments, "job_id")?;
        let config_path = resolve_config(arguments, job_id.as_deref())?;
        if self.mode == Boundary::Remote {
            return self.launch_remote_study(&config_path, job_id.as_deref());
        }
        if let Some(submitter) = &self.study_submitter {
            let workspace = workspace_option(arguments)?;
            return submitter
                .submit(&config_path, workspace.as_deref(), job_id.as_deref())
                .map_err(|error| {
                    ToolExecutionError::new(format!(
                        "the local study runner could not complete the study: {error}"
                    ))
                });
        }
        Err(ToolExecutionError::new(
            "local run_study requires its durable submission capability",
        ))
    }

    /// Launch one screening run through the selected local or remote boundary.
    ///
    /// A screening run evaluates one JIT-authored cross-sectional program and
    /// produces a ranked `screen.json` artifact. The local boundary returns the
    /// job id immediately and completes the run in the background; the
    /// remote boundary posts the configuration to the screener launch route.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the boundary fails.
    pub(super) fn run_screener(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = optional_identifier_argument(arguments, "job_id")?;
        let config_path = resolve_config(arguments, job_id.as_deref())?;
        if self.mode == Boundary::Remote {
            return self.launch_remote_screener(&config_path, job_id.as_deref());
        }
        if let Some(submitter) = &self.screening_submitter {
            let workspace = workspace_option(arguments)?;
            return submitter
                .submit(&config_path, workspace.as_deref(), job_id.as_deref())
                .map_err(|error| {
                    ToolExecutionError::new(format!(
                        "the local screening runner could not complete the screening run: {error}"
                    ))
                });
        }
        Err(ToolExecutionError::new(
            "local run_screener requires its durable submission capability",
        ))
    }

    /// Run one supervised paper trading session locally.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the process boundary fails.
    pub(super) fn launch_trader(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let config_path = text_argument(arguments, "config_path")?;
        self.invoke_process(
            &["trader".to_string(), "--config".to_string(), config_path],
            "trader",
        )
    }

    pub(super) fn invoke_process(
        &self,
        tail: &[String],
        process: &str,
    ) -> Result<Value, ToolExecutionError> {
        let Some(runner) = &self.runner else {
            return Err(ToolExecutionError::new(format!(
                "the local harness process '{process}' could not be invoked: \
                 no process runner is composed"
            )));
        };
        let mut argv = self.program.clone();
        argv.extend(tail.iter().cloned());
        let outcome = runner.run(&argv).map_err(|_| {
            ToolExecutionError::new(format!(
                "the local harness process '{process}' could not be invoked"
            ))
        })?;
        let payload = json!({
            "boundary": "local",
            "exit_code": outcome.returncode,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
        });
        if outcome.returncode != 0 {
            return Err(ToolExecutionError::with_payload(
                format!(
                    "the local harness process '{process}' exited with code {}",
                    outcome.returncode
                ),
                payload,
            ));
        }
        Ok(payload)
    }

    pub(super) fn launch_remote_backtest(
        &self,
        config_path: &str,
        job_id: Option<&str>,
    ) -> Result<Value, ToolExecutionError> {
        let mut payload = self
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the backtest configuration '{config_path}': {error}"
                ))
            })?;
        harness_research::configuration::overlay_job_id_json(&mut payload, job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let url = self.remote.remote_url(LAUNCH_ROUTE)?;
        validate_configuration(&payload, std::path::Path::new(config_path).parent())?;
        let response = self.remote.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if status_code == 404 || status_code == 501 {
            return Err(remote_response_failure(
                &response,
                "the remote harness API does not implement backtest launch",
            ));
        }
        if !(200..300).contains(&status_code) {
            return Err(remote_response_failure(
                &response,
                "remote backtest launch failed",
            ));
        }
        receipt(
            response,
            payload["job_id"].as_str().unwrap_or_default(),
            "backtest",
        )
    }

    pub(super) fn launch_remote_study(
        &self,
        config_path: &str,
        job_id: Option<&str>,
    ) -> Result<Value, ToolExecutionError> {
        let mut payload = self
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the study configuration '{config_path}': {error}"
                ))
            })?;
        harness_research::configuration::overlay_job_id_json(&mut payload, job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let url = self.remote.remote_url(STUDY_LAUNCH_ROUTE)?;
        validate_configuration(&payload, std::path::Path::new(config_path).parent())?;
        let response = self.remote.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if status_code == 404 || status_code == 501 {
            return Err(remote_response_failure(
                &response,
                "the remote harness API does not implement study launch",
            ));
        }
        if !(200..300).contains(&status_code) {
            return Err(remote_response_failure(
                &response,
                "remote study launch failed",
            ));
        }
        receipt(
            response,
            payload["job_id"].as_str().unwrap_or_default(),
            "study",
        )
    }

    pub(super) fn launch_remote_screener(
        &self,
        config_path: &str,
        job_id: Option<&str>,
    ) -> Result<Value, ToolExecutionError> {
        let mut payload = self
            .configuration_loader
            .load_json(config_path)
            .map_err(|error| {
                ToolExecutionError::new(format!(
                    "could not load the screening configuration '{config_path}': {error}"
                ))
            })?;
        harness_research::configuration::overlay_job_id_json(&mut payload, job_id)
            .map_err(|error| ToolExecutionError::new(error.to_string()))?;
        let url = self.remote.remote_url(SCREENER_LAUNCH_ROUTE)?;
        validate_configuration(&payload, std::path::Path::new(config_path).parent())?;
        let response = self.remote.dispatch_remote_post(&url, &payload)?;
        let status_code = response.status_code;
        if status_code == 401 || status_code == 403 {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if status_code == 404 || status_code == 501 {
            return Err(remote_response_failure(
                &response,
                "the remote harness API does not implement screening launch",
            ));
        }
        if !(200..300).contains(&status_code) {
            return Err(remote_response_failure(
                &response,
                "remote screening launch failed",
            ));
        }
        receipt(
            response,
            payload["job_id"].as_str().unwrap_or_default(),
            "screen",
        )
    }
}

fn receipt(
    response: RemoteResponse,
    expected: &str,
    kind: &str,
) -> Result<Value, ToolExecutionError> {
    let id = response
        .body
        .get("job_id")
        .and_then(Value::as_str)
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| {
            ToolExecutionError::with_payload(
                "remote harness response requires a non-empty job_id",
                response.body.clone(),
            )
            .with_certainty("unknown_outcome")
        })?;
    if id != expected {
        return Err(ToolExecutionError::with_payload(
            format!("remote acknowledgement job_id mismatch: expected {expected}, received {id}"),
            response.body.clone(),
        )
        .with_certainty("unknown_outcome"));
    }
    let state = response
        .body
        .get("state")
        .and_then(Value::as_str)
        .filter(|v| harness_execution::jobs::JobState::parse(v).is_ok())
        .ok_or_else(|| {
            ToolExecutionError::with_payload(
                "remote harness response requires a valid state",
                response.body.clone(),
            )
            .with_certainty("unknown_outcome")
        })?;
    Ok(json!({"boundary":"remote","job_id":id,"state":state,"kind":kind}))
}
