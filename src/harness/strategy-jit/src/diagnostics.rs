//! Classify structured Cargo diagnostics, never rendered error text.

use std::path::Path;
use std::path::PathBuf;

use serde::Deserialize;

#[derive(Deserialize)]
#[serde(tag = "reason")]
enum CargoRecord {
    #[serde(rename = "compiler-message")]
    CompilerMessage { target: Target, message: Message },
    #[serde(rename = "compiler-artifact")]
    CompilerArtifact {
        target: ArtifactTarget,
        filenames: Vec<PathBuf>,
    },
    #[serde(other)]
    Other,
}

#[derive(Deserialize)]
struct ArtifactTarget {
    src_path: PathBuf,
    crate_types: Vec<String>,
}

pub(super) fn build_artifact(stdout: &[u8], manifest: &Path) -> Option<PathBuf> {
    let source = manifest.parent()?.join("src/lib.rs");
    String::from_utf8_lossy(stdout).lines().find_map(|line| {
        let CargoRecord::CompilerArtifact { target, filenames } =
            serde_json::from_str::<CargoRecord>(line).ok()?
        else {
            return None;
        };
        if target.src_path != source || !target.crate_types.iter().any(|kind| kind == "cdylib") {
            return None;
        }
        filenames.into_iter().find(|path| {
            matches!(
                path.extension().and_then(|extension| extension.to_str()),
                Some("dll" | "so" | "dylib")
            )
        })
    })
}

#[derive(Deserialize)]
struct Target {
    src_path: PathBuf,
}

#[derive(Deserialize)]
struct Message {
    level: Level,
    rendered: Option<String>,
    spans: Vec<Span>,
}

#[derive(Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
enum Level {
    Error,
    #[serde(other)]
    Other,
}

#[derive(Deserialize)]
struct Span {
    is_primary: bool,
    file_name: PathBuf,
}

pub(super) fn output_tail(bytes: &[u8]) -> String {
    let filtered = String::from_utf8_lossy(bytes)
        .lines()
        .filter(|line| {
            let line = line.trim_start();
            ![
                "Compiling ",
                "Checking ",
                "Finished ",
                "Downloading ",
                "Downloaded ",
                "Updating ",
                "Locking ",
                "Adding ",
                "Blocking waiting for file lock",
                "For more information about this error, try `rustc --explain",
                "Some errors have detailed explanations:",
            ]
            .iter()
            .any(|prefix| line.starts_with(prefix))
        })
        .collect::<Vec<_>>()
        .join("\n");
    filtered
        .chars()
        .rev()
        .take(4000)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<String>()
        .trim()
        .to_string()
}

fn compact_diagnostic(message: &serde_json::Value) -> serde_json::Value {
    use serde_json::{json, Map, Value};
    let mut compact = Map::new();
    for key in ["message", "level"] {
        if let Some(value) = message.get(key) {
            compact.insert(key.to_string(), value.clone());
        }
    }
    if let Some(code) = message.get("code").and_then(|code| code.get("code")) {
        compact.insert("code".into(), json!({"code": code}));
    }
    if let Some(spans) = message.get("spans").and_then(Value::as_array) {
        compact.insert(
            "spans".into(),
            Value::Array(
                spans
                    .iter()
                    .map(|span| {
                        let mut location = Map::new();
                        for key in [
                            "file_name",
                            "line_start",
                            "line_end",
                            "column_start",
                            "column_end",
                            "is_primary",
                            "label",
                            "suggested_replacement",
                            "suggestion_applicability",
                        ] {
                            if let Some(value) = span.get(key).filter(|value| !value.is_null()) {
                                location.insert(key.to_string(), value.clone());
                            }
                        }
                        Value::Object(location)
                    })
                    .collect(),
            ),
        );
    }
    if let Some(children) = message.get("children").and_then(Value::as_array) {
        if !children.is_empty() {
            compact.insert(
                "children".into(),
                Value::Array(children.iter().map(compact_diagnostic).collect()),
            );
        }
    }
    Value::Object(compact)
}

pub(super) fn build_diagnostics(
    stdout: &[u8],
    manifest: &Path,
) -> (Vec<serde_json::Value>, String) {
    let mut diagnostics = Vec::new();
    let mut non_authored_error = false;
    let mut rendered = String::new();
    let source = manifest.parent().unwrap().join("src/lib.rs");
    for line in String::from_utf8_lossy(stdout).lines() {
        let Ok(CargoRecord::CompilerMessage { target, message }) =
            serde_json::from_str::<CargoRecord>(line)
        else {
            continue;
        };
        if message.level != Level::Error {
            continue;
        }
        if let Some(text) = &message.rendered {
            rendered.push_str(text);
        }
        let authored = target.src_path == source
            && message.spans.iter().any(|span| {
                span.is_primary
                    && (span.file_name == source
                        || span.file_name.to_string_lossy().replace('\\', "/") == "src/lib.rs")
            });
        if authored {
            let record: serde_json::Value = serde_json::from_str(line).unwrap();
            diagnostics.push(compact_diagnostic(&record["message"]));
        } else {
            non_authored_error = true;
        }
    }
    if non_authored_error {
        diagnostics.clear();
    }
    (diagnostics, rendered)
}

#[cfg(test)]
#[path = "../tests/unit/diagnostics.rs"]
mod tests;

#[cfg(test)]
#[path = "../tests/unit/diagnostic_compactness.rs"]
mod compactness_tests;
