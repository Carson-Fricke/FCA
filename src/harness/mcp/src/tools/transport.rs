//! Owns MCP transport behavior over the injected capabilities; no host composition.
use super::*;
pub(super) struct RemoteClient {
    pub(super) credentials: Arc<dyn BearerTokenSource>,
    pub(super) api_base_url: Option<String>,
    pub(super) remote_transport: Option<Arc<dyn RemoteTransport>>,
}
impl RemoteClient {
    pub(super) fn remote_validation(
        &self,
        name: &str,
        model: &Value,
    ) -> Result<Value, ToolExecutionError> {
        self.remote_tool(name, &json!({"model": model}))
    }

    pub(super) fn remote_tool(
        &self,
        name: &str,
        arguments: &Value,
    ) -> Result<Value, ToolExecutionError> {
        let url = self.remote_url("/mcp")?;
        let response = self.dispatch_remote_post(
            &url,
            &json!({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}
            }),
        )?;
        if matches!(response.status_code, 401 | 403) {
            return Err(remote_response_failure(
                &response,
                "remote credential rejected",
            ));
        }
        if !(200..300).contains(&response.status_code) {
            return Err(remote_response_failure(
                &response,
                &format!("remote {name} failed"),
            ));
        }
        let body = response.body;
        if body["jsonrpc"] != "2.0" || body["id"] != 1 {
            return Err(ToolExecutionError::new(
                "remote validation returned an invalid JSON-RPC envelope",
            )
            .with_certainty("unknown_outcome"));
        }
        if let Some(error) = body.get("error") {
            let certainty = error
                .get("data")
                .and_then(|data| data.get("executionCertainty"))
                .and_then(Value::as_str)
                .filter(|value| {
                    matches!(
                        *value,
                        "not_started" | "observed_failure" | "unknown_outcome"
                    )
                })
                .unwrap_or(
                    if matches!(
                        error["code"].as_i64(),
                        Some(-32700 | -32600 | -32601 | -32602)
                    ) {
                        "not_started"
                    } else {
                        "unknown_outcome"
                    },
                );
            return Err(ToolExecutionError::with_payload(
                error["message"]
                    .as_str()
                    .unwrap_or("remote validation protocol failure"),
                json!({"serverError": error}),
            )
            .with_certainty(certainty));
        }
        let result = &body["result"];
        let payload = &result["structuredContent"];
        if result["isError"] == true {
            let text = result["content"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|block| block["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n");
            return Err(ToolExecutionError::with_payload(
                if text.is_empty() {
                    "remote validation failed".to_string()
                } else {
                    text
                },
                payload.clone(),
            ));
        }
        if result["isError"] != false
            || !payload.is_object()
            || (name != TOOL_READ_SDK_REFERENCE && payload["valid"] != true)
        {
            return Err(ToolExecutionError::new(
                "remote validation returned no valid structured outcome",
            )
            .with_certainty("unknown_outcome"));
        }
        Ok(payload.clone())
    }

    pub(super) fn remote_get(
        &self,
        url: &str,
        not_found_message: &str,
    ) -> Result<Value, ToolExecutionError> {
        let response = self.dispatch_remote_get(url)?;
        match response.status_code {
            200 => Ok(response.body),
            401 | 403 => Err(credential_rejected(response.status_code)),
            404 => Err(ToolExecutionError::new(not_found_message)),
            other => Err(ToolExecutionError::new(format!(
                "the remote harness API returned HTTP {other}"
            ))),
        }
    }

    pub(super) fn dispatch_remote_get(
        &self,
        url: &str,
    ) -> Result<RemoteResponse, ToolExecutionError> {
        let headers = self.remote_headers()?;
        let transport = self
            .remote_transport
            .as_ref()
            .ok_or_else(remote_unconfigured)?;
        transport
            .get(url, &headers)
            .map_err(|_| ToolExecutionError::new("the remote harness API could not be reached"))
    }

    pub(super) fn dispatch_remote_post(
        &self,
        url: &str,
        payload: &Value,
    ) -> Result<RemoteResponse, ToolExecutionError> {
        let headers = self.remote_headers()?;
        let transport = self
            .remote_transport
            .as_ref()
            .ok_or_else(remote_unconfigured)?;
        transport.post(url, &headers, payload).map_err(|_| {
            ToolExecutionError::new(
                "the remote harness API could not be reached; request outcome is unknown",
            )
            .with_certainty("unknown_outcome")
        })
    }

    pub(super) fn remote_headers(&self) -> Result<BTreeMap<String, String>, ToolExecutionError> {
        self.credentials.bearer_headers().map_err(|error| {
            ToolExecutionError::new(format!(
                "remote harness API calls need a bearer credential: {error}"
            ))
            .with_certainty("not_started")
        })
    }

    pub(super) fn remote_url(&self, route: &str) -> Result<String, ToolExecutionError> {
        let base_url = self.api_base_url.as_ref().ok_or_else(remote_unconfigured)?;
        Ok(format!("{base_url}{route}"))
    }
}
