//! Newline-delimited JSON framing for the harness MCP stdio server.
//!
//! Framing decision: one JSON-RPC message per line, compact JSON, with
//! diagnostics on stderr only. Malformed lines produce a JSON-RPC parse error
//! response with a `null` id; EOF closes the loop.

use std::fmt;
use std::io::{BufRead, Write};

use serde_json::{json, Value};

use crate::server::{error_response, McpServer, PARSE_ERROR};

/// Raised when one stdin line is not decodable JSON.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FramingError(pub String);

impl fmt::Display for FramingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "line is not valid JSON: {}", self.0)
    }
}

impl std::error::Error for FramingError {}

/// Render one response as a single-line JSON document.
#[must_use]
pub fn encode_message(message: &Value) -> String {
    serde_json::to_string(message).unwrap_or_else(|_| "{}".to_string())
}

/// Decode one stdin line, failing when it is not valid JSON.
///
/// # Errors
///
/// Returns [`FramingError`] when the line is malformed.
pub fn decode_message(line: &str) -> Result<Value, FramingError> {
    serde_json::from_str(line).map_err(|error| FramingError(error.to_string()))
}

fn parse_error_response(error: &FramingError) -> Value {
    error_response(
        &Value::Null,
        PARSE_ERROR,
        "parse error: the request line is not valid JSON",
        Some(json!({"detail": error.to_string()})),
    )
}

/// Serve newline-delimited JSON-RPC over stdio until EOF.
///
/// Blank lines are skipped, malformed lines answer with one parse-error
/// response, and notifications produce no output. Returns the number of
/// responses written.
///
/// # Errors
///
/// Returns an I/O error when reading or writing fails.
pub fn serve_stdio<R: BufRead, W: Write, E: Write>(
    server: &McpServer,
    reader: R,
    writer: &mut W,
    diagnostics: &mut E,
) -> std::io::Result<usize> {
    writeln!(diagnostics, "harness mcp: serving JSON-RPC over stdio")?;
    diagnostics.flush()?;
    let mut answered = 0usize;
    for line in reader.lines() {
        let line = line?;
        let text = line.trim();
        if text.is_empty() {
            continue;
        }
        let response = match decode_message(text) {
            Ok(request) => server.handle(&request),
            Err(error) => Some(parse_error_response(&error)),
        };
        if let Some(response) = response {
            writeln!(writer, "{}", encode_message(&response))?;
            writer.flush()?;
            answered += 1;
        }
    }
    writeln!(
        diagnostics,
        "harness mcp: shutdown after {answered} responses"
    )?;
    diagnostics.flush()?;
    Ok(answered)
}

/// Test seam that dispatches decoded requests without framing.
pub struct InProcessTransport {
    server: McpServer,
}

impl InProcessTransport {
    /// Wrap one server for direct dispatch.
    #[must_use]
    pub fn new(server: McpServer) -> Self {
        Self { server }
    }

    /// Dispatch one decoded request exactly as the stdio loop would.
    #[must_use]
    pub fn call(&self, request: &Value) -> Option<Value> {
        self.server.handle(request)
    }

    /// The wrapped server.
    #[must_use]
    pub fn server(&self) -> &McpServer {
        &self.server
    }
}
