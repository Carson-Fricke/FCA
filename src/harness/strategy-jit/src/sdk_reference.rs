//! Read-only SDK source inventory, identity, metadata and bounded references.
//!
//! CRLF and LF share one identity; every other byte and relative filename remains
//! significant. Compiler provenance and validation-result caching stay outside.

use std::collections::BTreeMap;
use std::path::Path;

use harness_contracts::sha256::digest_hex;
use serde_json::json;

use crate::error::JitError;

fn source_digest(bytes: &[u8]) -> String {
    let mut canonical = Vec::with_capacity(bytes.len());
    for (index, &byte) in bytes.iter().enumerate() {
        if byte != b'\r' || bytes.get(index + 1) != Some(&b'\n') {
            canonical.push(byte);
        }
    }
    digest_hex(&canonical)
}

pub(crate) fn reference(
    sdk: &Path,
    arguments: &serde_json::Map<String, serde_json::Value>,
) -> Result<serde_json::Value, JitError> {
    let mut hashes = BTreeMap::new();
    for (name, root) in [
        ("strategy-sdk", sdk.to_path_buf()),
        ("contracts", sdk.join("../contracts")),
        ("num", sdk.join("../num")),
        ("lake-contracts", sdk.join("../../lake/contracts")),
    ] {
        hashes.insert(
            format!("{name}/Cargo.toml"),
            source_digest(&std::fs::read(root.join("Cargo.toml"))?),
        );
        let mut count = 0;
        let mut directories = vec![root.join("src")];
        while let Some(directory) = directories.pop() {
            for entry in std::fs::read_dir(directory)? {
                let path = entry?.path();
                if path.is_dir() {
                    directories.push(path);
                } else if path.is_file()
                    && path.extension().is_some_and(|extension| extension == "rs")
                {
                    let relative = path
                        .strip_prefix(&root)
                        .map_err(|e| JitError::Invalid(e.to_string()))?;
                    let file = relative
                        .to_str()
                        .ok_or_else(|| {
                            JitError::Invalid("SDK source filename is not UTF-8".into())
                        })?
                        .replace('\\', "/");
                    hashes.insert(
                        format!("{name}/{file}"),
                        source_digest(&std::fs::read(&path)?),
                    );
                    count += 1;
                }
            }
        }
        if count == 0 {
            return Err(JitError::Invalid(format!(
                "SDK reference sources missing for {name}"
            )));
        }
    }
    let identity =
        digest_hex(&serde_json::to_vec(&hashes).map_err(|error| JitError::Io(error.to_string()))?);
    let manifest = std::fs::read_to_string(sdk.join("Cargo.toml"))?;
    let abi = std::fs::read_to_string(sdk.join("src/abi.rs"))?;
    let version = manifest
        .split("[package]")
        .nth(1)
        .and_then(|package| package.split('[').next())
        .and_then(|package| {
            package.lines().find_map(|line| {
                let (key, value) = line.split_once('=')?;
                (key.trim() == "version").then(|| value.trim().trim_matches('"').to_string())
            })
        })
        .ok_or_else(|| JitError::Invalid("SDK version metadata is malformed".into()))?;
    let abi_version = abi
        .split("SDK_ABI_VERSION: u32 =")
        .nth(1)
        .and_then(|value| value.split(';').next())
        .and_then(|value| value.trim().parse::<u32>().ok())
        .ok_or_else(|| JitError::Invalid("SDK ABI metadata is malformed".into()))?;
    let mut result = json!({
        "sdkIdentity": identity,
        "sdkVersion": version,
        "sdkAbiVersion": abi_version,
        "sourceHashes": hashes,
    });
    if let Some(topic) = arguments.get("topic") {
        let topic = topic
            .as_str()
            .ok_or_else(|| JitError::Invalid("topic must be a string".into()))?;
        let (file, hash_key) = match topic {
            "consumer" => ("src/consumer.rs", "strategy-sdk/src/consumer.rs"),
            "program" => ("src/program.rs", "strategy-sdk/src/program.rs"),
            "outputs" => ("src/outputs.rs", "strategy-sdk/src/outputs.rs"),
            "research_views" => ("src/research/view.rs", "strategy-sdk/src/research/view.rs"),
            "research_publishing" => (
                "src/research/publishing.rs",
                "strategy-sdk/src/research/publishing.rs",
            ),
            "research_recipes" => (
                "src/research/recipes.rs",
                "strategy-sdk/src/research/recipes.rs",
            ),
            "building_blocks" => (
                "src/building_blocks.rs",
                "strategy-sdk/src/building_blocks.rs",
            ),
            "indicator_api" => ("src/indicator.rs", "strategy-sdk/src/indicator.rs"),
            "wavelet" => ("src/wavelet.rs", "strategy-sdk/src/wavelet.rs"),
            "events" => ("src/events.rs", "strategy-sdk/src/events.rs"),
            "exports" => ("src/export.rs", "strategy-sdk/src/export.rs"),
            "decimal" => ("../num/src/decimal.rs", "num/src/decimal.rs"),
            _ => return Err(JitError::Invalid("unknown SDK reference topic".into())),
        };
        let number = |key: &str, default: u64| -> Result<usize, JitError> {
            let value = match arguments.get(key) {
                Some(value) => value.as_u64().ok_or_else(|| {
                    JitError::Invalid(format!("{key} must be a positive integer"))
                })?,
                None => default,
            };
            usize::try_from(value)
                .ok()
                .filter(|value| *value > 0)
                .ok_or_else(|| JitError::Invalid(format!("{key} must be a positive integer")))
        };
        let start = number("start_line", 1)?;
        let max = number("max_chars", 16000)?;
        if max > 16000 {
            return Err(JitError::Invalid("max_chars must be at most 16000".into()));
        }
        let text = std::fs::read_to_string(sdk.join(file))?;
        if source_digest(text.as_bytes()) != hashes[hash_key] {
            return Err(JitError::Invalid(
                "SDK reference changed while reading; request again".into(),
            ));
        }
        let lines: Vec<_> = text.lines().collect();
        let end = number("end_line", lines.len() as u64)?;
        if start > end || end > lines.len() {
            return Err(JitError::Invalid(
                "SDK line range is outside the source".into(),
            ));
        }
        let selected = lines[start - 1..end].join("\n");
        let content: String = selected.chars().take(max).collect();
        result["topic"] = json!(topic);
        result["content"] = json!(content);
        result["startLine"] = json!(start);
        result["endLine"] = json!(end);
        result["totalLines"] = json!(lines.len());
        result["truncated"] = json!(selected.chars().count() > max);
    }
    Ok(result)
}

#[cfg(test)]
#[path = "../tests/unit/sdk_reference.rs"]
mod tests;
