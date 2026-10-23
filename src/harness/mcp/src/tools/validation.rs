//! Owns MCP validation behavior over the injected capabilities; no host composition.
use super::*;
pub(super) struct ValidationTools {
    pub(super) mode: Boundary,
    pub(super) study_validator: Option<Arc<dyn StudyValidator>>,
    pub(super) sdk_reference: Option<Arc<dyn SdkReference>>,
    pub(super) remote: Arc<transport::RemoteClient>,
}
impl ValidationTools {
    /// Validate one authored model document without replaying history.
    ///
    /// The model's program is compiled, loaded, and constructed exactly as a
    /// run would, so a source that cannot build or omits the SDK handshake
    /// exports fails here while the caller can still repair it. The model
    /// document is the same object the completion contract carries; the
    /// platform builds the runtime study job from it internally.
    ///
    /// # Errors
    ///
    /// Returns [`ToolExecutionError`] when the model document is malformed or
    /// the authored program fails to build.
    pub(super) fn validate_study(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let Some(model) = arguments.get("model") else {
            return Err(ToolExecutionError::new(
                "validate_study requires the model object under 'model'",
            ));
        };
        if self.mode == Boundary::Remote {
            return self.remote.remote_validation(TOOL_VALIDATE_STUDY, model);
        }
        let Some(validator) = &self.study_validator else {
            return Err(ToolExecutionError::new(
                "study validation requires a local study validator in this MCP server",
            ));
        };
        validator.validate_result(model)
    }

    /// Validate a strategy through the same injected native compiler boundary.
    pub(super) fn validate_strategy(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        let model = arguments.get("model").ok_or_else(|| {
            ToolExecutionError::new("validate_strategy requires the model object under 'model'")
        })?;
        if self.mode == Boundary::Remote {
            return self.remote.remote_validation(TOOL_VALIDATE_STRATEGY, model);
        }
        self.study_validator
            .as_ref()
            .ok_or_else(|| {
                ToolExecutionError::new(
                    "strategy validation requires a local program validator in this MCP server",
                )
            })?
            .validate_strategy_result(model)
    }

    /// Read metadata or a bounded source range from the serving compiler's SDK.
    pub(super) fn read_sdk_reference(
        &self,
        arguments: &Map<String, Value>,
    ) -> Result<Value, ToolExecutionError> {
        if self.mode == Boundary::Remote {
            return self
                .remote
                .remote_tool(TOOL_READ_SDK_REFERENCE, &Value::Object(arguments.clone()));
        }
        self.sdk_reference
            .as_ref()
            .ok_or_else(|| {
                ToolExecutionError::new("SDK references require a native program validator")
            })?
            .sdk_reference(arguments)
    }
}
