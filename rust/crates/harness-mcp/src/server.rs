//! JSON-RPC 2.0 / MCP protocol dispatch for the harness MCP server.
//!
//! The server is deterministic and pure: [`McpServer::handle`] takes one
//! decoded request and returns one response, or `None` for notifications.
//! Domain failures become `isError` results; protocol problems become
//! JSON-RPC error objects with precise codes.

use std::fmt;

use serde_json::{json, Map, Value};

use crate::tools::{build_catalog, ContextError, HarnessToolsContext, ToolCatalog};

/// Fixed MCP protocol version.
pub const PROTOCOL_VERSION: &str = "2024-11-05";
/// Default server name.
pub const SERVER_NAME: &str = "harness-mcp";
/// Default server version.
pub const SERVER_VERSION: &str = "0.1.0";

/// JSON-RPC parse error.
pub const PARSE_ERROR: i64 = -32700;
/// JSON-RPC invalid request.
pub const INVALID_REQUEST: i64 = -32600;
/// JSON-RPC method not found.
pub const METHOD_NOT_FOUND: i64 = -32601;
/// JSON-RPC invalid params.
pub const INVALID_PARAMS: i64 = -32602;
/// JSON-RPC internal error.
pub const INTERNAL_ERROR: i64 = -32603;

const JSONRPC_VERSION: &str = "2.0";

/// A protocol-level failure rendered as a JSON-RPC error object.
#[derive(Debug, Clone, PartialEq)]
pub struct JsonRpcError {
    /// JSON-RPC error code.
    pub code: i64,
    /// Human-readable message.
    pub message: String,
    /// Optional structured data.
    pub data: Option<Value>,
}

impl JsonRpcError {
    /// Construct an error.
    #[must_use]
    pub fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            data: None,
        }
    }

    /// Attach structured data.
    #[must_use]
    pub fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }
}

impl fmt::Display for JsonRpcError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for JsonRpcError {}

/// Build one JSON-RPC error response with an omitted-when-null data slot.
#[must_use]
pub fn error_response(request_id: &Value, code: i64, message: &str, data: Option<Value>) -> Value {
    let mut error = json!({"code": code, "message": message});
    if let (Some(data), Some(object)) = (data, error.as_object_mut()) {
        object.insert("data".to_string(), data);
    }
    json!({"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error})
}

fn valid_request_id(value: &Value) -> bool {
    matches!(value, Value::String(_)) || value.as_i64().is_some() || value.as_u64().is_some()
}

fn response_id(value: Option<&Value>) -> Value {
    match value {
        Some(value) if valid_request_id(value) => value.clone(),
        _ => Value::Null,
    }
}

/// Render a JSON value with sorted object keys and compact separators.
#[must_use]
pub fn dumps_sorted(value: &Value) -> String {
    let mut output = String::new();
    write_sorted(value, &mut output);
    output
}

fn write_sorted(value: &Value, output: &mut String) {
    match value {
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            output.push('{');
            for (index, (key, item)) in entries.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(&Value::String((*key).clone()).to_string());
                output.push(':');
                write_sorted(item, output);
            }
            output.push('}');
        }
        Value::Array(items) => {
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                write_sorted(item, output);
            }
            output.push(']');
        }
        other => output.push_str(&other.to_string()),
    }
}

fn validate_arguments(schema: &Value, arguments: &Map<String, Value>) -> Result<(), JsonRpcError> {
    let properties = schema
        .get("properties")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if schema.get("additionalProperties").and_then(Value::as_bool) == Some(false) {
        for name in arguments.keys() {
            if !properties.contains_key(name) {
                return Err(JsonRpcError::new(
                    INVALID_PARAMS,
                    format!("unexpected argument '{name}'"),
                ));
            }
        }
    }
    if let Some(required) = schema.get("required").and_then(Value::as_array) {
        for name in required {
            if let Some(name) = name.as_str() {
                if !arguments.contains_key(name) {
                    return Err(JsonRpcError::new(
                        INVALID_PARAMS,
                        format!("missing required argument '{name}'"),
                    ));
                }
            }
        }
    }
    for (name, value) in arguments {
        let Some(property) = properties.get(name).and_then(Value::as_object) else {
            continue;
        };
        if property.get("type").and_then(Value::as_str) == Some("string") && !value.is_string() {
            return Err(JsonRpcError::new(
                INVALID_PARAMS,
                format!("argument '{name}' must be a string"),
            ));
        }
        if property.get("type").and_then(Value::as_str) == Some("object") && !value.is_object() {
            return Err(JsonRpcError::new(
                INVALID_PARAMS,
                format!("argument '{name}' must be an object"),
            ));
        }
        if let (Some(text), Some(minimum)) = (
            value.as_str(),
            property.get("minLength").and_then(Value::as_u64),
        ) {
            if (text.chars().count() as u64) < minimum {
                return Err(JsonRpcError::new(
                    INVALID_PARAMS,
                    format!("argument '{name}' must not be empty"),
                ));
            }
        }
        if let (Some(text), Some(maximum)) = (
            value.as_str(),
            property.get("maxLength").and_then(Value::as_u64),
        ) {
            if (text.chars().count() as u64) > maximum {
                return Err(JsonRpcError::new(
                    INVALID_PARAMS,
                    format!("argument '{name}' must be at most {maximum} characters"),
                ));
            }
        }
    }
    Ok(())
}

/// Dispatch one MCP request mapping to one response mapping.
pub struct McpServer {
    /// Published tool catalog.
    pub catalog: ToolCatalog,
    /// Server name reported by `initialize`.
    pub server_name: String,
    /// Server version reported by `initialize`.
    pub server_version: String,
    /// Protocol version reported by `initialize`.
    pub protocol_version: String,
}

impl McpServer {
    /// Dispatch one decoded request; notifications answer `None`.
    #[must_use]
    pub fn handle(&self, request: &Value) -> Option<Value> {
        let Some(object) = request.as_object() else {
            return Some(error_response(
                &Value::Null,
                INVALID_REQUEST,
                "a request must be a JSON object",
                None,
            ));
        };
        if object.get("jsonrpc").and_then(Value::as_str) != Some(JSONRPC_VERSION) {
            return Some(error_response(
                &response_id(object.get("id")),
                INVALID_REQUEST,
                "jsonrpc must be the exact string '2.0'",
                None,
            ));
        }
        let Some(method) = object
            .get("method")
            .and_then(Value::as_str)
            .filter(|m| !m.is_empty())
        else {
            return Some(error_response(
                &response_id(object.get("id")),
                INVALID_REQUEST,
                "method must be a non-empty string",
                None,
            ));
        };
        let raw_id = object.get("id");
        if object.contains_key("id") && !raw_id.is_some_and(valid_request_id) {
            return Some(error_response(
                &Value::Null,
                INVALID_REQUEST,
                "id must be a string or an integer",
                None,
            ));
        }
        let params = object.get("params").cloned().unwrap_or_else(|| json!({}));
        let Some(params) = params.as_object() else {
            return Some(error_response(
                &response_id(raw_id),
                INVALID_REQUEST,
                "params must be a JSON object when present",
                None,
            ));
        };
        let notification = !object.contains_key("id");
        match self.dispatch(method, params) {
            Ok(result) => {
                if notification {
                    None
                } else {
                    Some(
                        json!({"jsonrpc": JSONRPC_VERSION, "id": raw_id.cloned().unwrap_or(Value::Null), "result": result}),
                    )
                }
            }
            Err(error) => {
                if notification {
                    None
                } else {
                    Some(error_response(
                        &response_id(raw_id),
                        error.code,
                        &error.message,
                        error.data,
                    ))
                }
            }
        }
    }

    /// Return the serialized server description; it carries no secrets.
    #[must_use]
    pub fn describe(&self) -> Value {
        json!({
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": false}},
            "serverInfo": {"name": self.server_name, "version": self.server_version},
            "tools": self.catalog.names(),
        })
    }

    fn dispatch(&self, method: &str, params: &Map<String, Value>) -> Result<Value, JsonRpcError> {
        match method {
            "initialize" => Ok(json!({
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": false}},
                "serverInfo": {"name": self.server_name, "version": self.server_version},
            })),
            "ping" => Ok(json!({})),
            "tools/list" => Ok(json!({
                "tools": self.catalog.tools().iter().map(|tool| tool.schema_entry()).collect::<Vec<_>>(),
            })),
            "tools/call" => self.call_tool(params),
            other => Err(JsonRpcError::new(
                METHOD_NOT_FOUND,
                format!("unknown method '{other}'"),
            )),
        }
    }

    fn call_tool(&self, params: &Map<String, Value>) -> Result<Value, JsonRpcError> {
        let name = params
            .get("name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .ok_or_else(|| {
                JsonRpcError::new(
                    INVALID_PARAMS,
                    "tools/call requires a non-empty 'name' string",
                )
            })?;
        let tool = self.catalog.resolve(name).ok_or_else(|| {
            JsonRpcError::new(
                INVALID_PARAMS,
                format!(
                    "unknown tool '{name}'; published tools: {:?}",
                    self.catalog.names()
                ),
            )
        })?;
        let arguments = params
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let Some(arguments) = arguments.as_object() else {
            return Err(JsonRpcError::new(
                INVALID_PARAMS,
                "tool arguments must be a JSON object",
            ));
        };
        validate_arguments(&tool.input_schema, arguments)?;
        match tool.call(arguments) {
            Ok(payload) => {
                let Some(payload) = payload.as_object() else {
                    return Err(JsonRpcError::new(
                        INTERNAL_ERROR,
                        format!("tool '{name}' returned a non-object payload"),
                    ));
                };
                let content = dumps_sorted(&Value::Object(payload.clone()));
                Ok(json!({
                    "content": [{"type": "text", "text": content}],
                    "isError": false,
                    "structuredContent": payload,
                }))
            }
            Err(error) => {
                let mut result = json!({
                    "content": [{"type": "text", "text": error.message()}],
                    "isError": true,
                });
                if let (Some(payload), Some(object)) = (error.payload(), result.as_object_mut()) {
                    object.insert("structuredContent".to_string(), payload.clone());
                }
                Ok(result)
            }
        }
    }
}

/// Compose one server from an injected boundary context.
///
/// # Errors
///
/// Returns [`ContextError`] when the tool catalog cannot be composed.
pub fn create_server(context: HarnessToolsContext) -> Result<McpServer, ContextError> {
    create_server_named(context, SERVER_NAME, SERVER_VERSION, PROTOCOL_VERSION)
}

/// Compose one server with explicit identity strings.
///
/// # Errors
///
/// Returns [`ContextError`] when the tool catalog cannot be composed.
pub fn create_server_named(
    context: HarnessToolsContext,
    server_name: &str,
    server_version: &str,
    protocol_version: &str,
) -> Result<McpServer, ContextError> {
    Ok(McpServer {
        catalog: build_catalog(context)?,
        server_name: server_name.to_string(),
        server_version: server_version.to_string(),
        protocol_version: protocol_version.to_string(),
    })
}
