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
    let root = sdk.canonicalize()?;
    let sdk = root.as_path();
    for key in arguments.keys() {
        if ![
            "topic",
            "start_line",
            "end_line",
            "token_offset",
            "max_tokens",
        ]
        .contains(&key.as_str())
        {
            return Err(JitError::Invalid(format!("unknown SDK reference argument {key:?}; use topic, line ranges, token_offset and max_tokens")));
        }
    }
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
    let alias_bytes = std::fs::read(sdk.join("reference-topics.json"))?;
    let aliases: BTreeMap<String, String> = serde_json::from_slice(&alias_bytes)
        .map_err(|error| JitError::Invalid(error.to_string()))?;
    if aliases.values().any(|path| !hashes.contains_key(path)) {
        return Err(JitError::Invalid(
            "SDK reference aliases do not match source inventory".into(),
        ));
    }
    hashes.insert(
        "strategy-sdk/reference-topics.json".into(),
        source_digest(&alias_bytes),
    );
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
        let hash_key = aliases.get(topic).map_or(topic, String::as_str);
        if !hashes.contains_key(hash_key) || !hash_key.ends_with(".rs") {
            return Err(JitError::Invalid(format!(
                "unknown SDK reference {topic:?}; select a sourceHashes path or an SDK topic"
            )));
        }
        let file = if let Some(relative) = hash_key.strip_prefix("lake-contracts/") {
            sdk.parent()
                .and_then(Path::parent)
                .expect("SDK root")
                .join("lake/contracts")
                .join(relative)
        } else {
            sdk.parent().expect("SDK root").join(hash_key)
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
        let max = number("max_tokens", 16384)?;
        if !(32..=65536).contains(&max) {
            return Err(JitError::Invalid(
                "max_tokens must be between 32 and 65536".into(),
            ));
        }
        let text = std::fs::read_to_string(sdk.join(file))?.replace("\r\n", "\n");
        if source_digest(text.as_bytes()) != hashes[hash_key] {
            return Err(JitError::Invalid(
                "SDK reference changed while reading; request again".into(),
            ));
        }
        let lines: Vec<_> = text.split_inclusive('\n').collect();
        let end = number("end_line", lines.len() as u64)?;
        if start > end || end > lines.len() {
            return Err(JitError::Invalid(format!("SDK range must satisfy 1 <= start_line <= end_line <= {}; omit end_line to read to the end", lines.len())));
        }
        let offset = match arguments.get("token_offset") {
            None => 0,
            Some(value) => value
                .as_u64()
                .and_then(|v| usize::try_from(v).ok())
                .ok_or_else(|| {
                    JitError::Invalid("token_offset must be a non-negative integer".into())
                })?,
        };
        let selected = lines[start - 1..end].concat();
        let codec = tiktoken_rs::cl100k_base_singleton();
        let tokens = codec.encode_ordinary(&selected);
        if offset > tokens.len() || codec.decode(&tokens[..offset]).is_err() {
            return Err(JitError::Invalid(format!(
                "token_offset must address a UTF-8 boundary within 0..={}; use nextArguments",
                tokens.len()
            )));
        }
        let cost = |value: &str| {
            codec
                .encode_ordinary(&serde_json::to_string(value).expect("string serialization"))
                .len()
        };
        let mut stop = tokens.len();
        let mut content = codec
            .decode(&tokens[offset..])
            .map_err(|e| JitError::Invalid(e.to_string()))?;
        let mut token_count = cost(&content);
        if token_count > max {
            stop = stop.min(offset + max * 3 / 4);
            loop {
                if stop <= offset {
                    return Err(JitError::Invalid(
                        "max_tokens cannot fit the next character; increase the budget".into(),
                    ));
                }
                if let Ok(value) = codec.decode(&tokens[offset..stop]) {
                    token_count = cost(&value);
                    if token_count <= max {
                        content = value;
                        break;
                    }
                    stop -= ((stop - offset) / 2)
                        .max(1)
                        .min(((token_count - max) / 2).max(1));
                } else {
                    stop -= 1;
                }
            }
        }
        result["topic"] = json!(topic);
        result["content"] = json!(content);
        result["startLine"] = json!(start);
        result["endLine"] = json!(end);
        result["totalLines"] = json!(lines.len());
        result["tokenizer"] = json!("cl100k_base");
        result["tokenOffset"] = json!(offset);
        result["totalTokens"] = json!(tokens.len());
        result["tokenCount"] = json!(token_count);
        result["complete"] = json!(offset == 0 && stop == tokens.len());
        result["truncated"] = json!(offset != 0 || stop != tokens.len());
        result["nextTokenOffset"] = if stop < tokens.len() {
            json!(stop)
        } else {
            serde_json::Value::Null
        };
        result["nextArguments"] = if stop < tokens.len() {
            json!({"topic": topic, "start_line": start, "end_line": end,
                   "token_offset": stop, "max_tokens": max})
        } else {
            serde_json::Value::Null
        };
    }
    Ok(result)
}

#[cfg(test)]
#[path = "../tests/unit/sdk_reference.rs"]
mod tests;
