//! Owns MCP queries behavior over the injected capabilities; no host composition.
use super::*;
pub(super) struct QueryTools {
    pub(super) mode: Boundary,
    pub(super) job_store: Option<Arc<dyn harness_execution::jobs::JobReader>>,
    pub(super) remote: Arc<transport::RemoteClient>,
}
impl QueryTools {
    /// Read one job's persisted state from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub(super) fn get_job_status(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        if self.mode == Boundary::Local {
            return self.local_job(&job_id);
        }
        let url = self.remote.remote_url(&format!(
            "{JOB_ROUTE}/{}",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote
            .remote_get(&url, &format!("job '{job_id}' does not exist"))
    }

    /// Read one stored job result from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub(super) fn get_job_result(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        if self.mode == Boundary::Local {
            return self.local_job(&job_id);
        }
        let url = self.remote.remote_url(&format!(
            "{JOB_ROUTE}/{}",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote
            .remote_get(&url, &format!("job '{job_id}' does not exist"))
    }

    /// List one stored job's artifacts from the remote harness API.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the remote call fails.
    pub(super) fn list_artifacts(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        if self.mode == Boundary::Local {
            return self.local_artifacts(&job_id);
        }
        let url = self.remote.remote_url(&format!(
            "{JOB_ROUTE}/{}/artifacts",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        self.remote
            .remote_get(&url, &format!("job '{job_id}' does not exist"))
    }

    /// Read one stored job artifact's contents as text or hex-encoded bytes.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the job or artifact does not exist,
    /// or the authenticated remote artifact response has mismatched attribution.
    pub(super) fn read_artifact(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let job_id = identifier_argument(arguments, "job_id")?;
        let name = text_argument(arguments, "name")?;
        if self.mode == Boundary::Local {
            return self.local_artifact(&job_id, &name);
        }
        let path = name
            .split('/')
            .map(|part| utf8_percent_encode(part, PATH_SEGMENT).to_string())
            .collect::<Vec<_>>()
            .join("/");
        let url = self.remote.remote_url(&format!(
            "{JOB_ROUTE}/{}/artifacts/{path}?representation=json",
            utf8_percent_encode(&job_id, PATH_SEGMENT)
        ))?;
        let document = self.remote.remote_get(
            &url,
            &format!("artifact '{name}' does not exist for job '{job_id}'"),
        )?;
        if document.get("job_id").and_then(Value::as_str) != Some(job_id.as_str())
            || document.get("name").and_then(Value::as_str) != Some(name.as_str())
        {
            return Err(ToolExecutionError::new(
                "remote artifact response attribution does not match request",
            ));
        }
        Ok(document)
    }

    pub(super) fn local_job(&self, job_id: &str) -> Result<Value, ToolExecutionError> {
        let store = self.job_store.as_ref().ok_or_else(|| {
            ToolExecutionError::new("local jobs require a durable job store in this MCP server")
        })?;
        match store
            .try_get(job_id)
            .map_err(|e| ToolExecutionError::new(e.to_string()))?
        {
            Some(record) => serde_json::to_value(&record)
                .map_err(|error| ToolExecutionError::new(error.to_string())),
            None => Err(ToolExecutionError::new(format!(
                "job '{job_id}' does not exist"
            ))),
        }
    }

    pub(super) fn local_artifacts(&self, job_id: &str) -> Result<Value, ToolExecutionError> {
        let store = self.job_store.as_ref().ok_or_else(|| {
            ToolExecutionError::new("local jobs require a durable job store in this MCP server")
        })?;
        let inventory = store
            .artifacts(job_id)
            .map_err(|e| ToolExecutionError::new(e.to_string()))?
            .ok_or_else(|| ToolExecutionError::new(format!("job '{job_id}' does not exist")))?;
        let artifacts:Vec<Value>=inventory.into_iter().map(|info|json!({"name":info.name,"uri":info.uri,"sha256":info.sha256,"sizeBytes":info.size_bytes,"sas_url":Value::Null})).collect();
        Ok(json!({"job_id":job_id,"artifacts":artifacts}))
    }
    pub(super) fn local_artifact(
        &self,
        job_id: &str,
        name: &str,
    ) -> Result<Value, ToolExecutionError> {
        let store = self
            .job_store
            .as_ref()
            .ok_or_else(|| ToolExecutionError::new("local jobs require a durable job store"))?;
        let payload = store
            .artifact(job_id, name)
            .map_err(|e| ToolExecutionError::new(e.to_string()))?
            .ok_or_else(|| {
                ToolExecutionError::new(format!(
                    "artifact '{name}' does not exist for job '{job_id}'"
                ))
            })?;
        Ok(match std::str::from_utf8(&payload) {
            Ok(content) => {
                json!({"job_id":job_id,"name":name,"encoding":"utf-8","content":content})
            }
            Err(_) => {
                json!({"job_id":job_id,"name":name,"encoding":"hex","content":hex::encode(payload)})
            }
        })
    }
}
