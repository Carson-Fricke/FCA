//! Canonical JSON encoding matching Python's
//! `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
//!
//! The recorder and backtest manifests are byte-for-byte artifacts of their
//! canonical JSON encoding, so the encoder must be exact: sorted object keys,
//! no insignificant whitespace, and ASCII-only output with `\uXXXX` escapes.

use std::collections::BTreeMap;
use std::fmt::Write as _;

/// A minimal JSON value with deterministic object-key ordering.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JsonValue {
    /// JSON null.
    Null,
    /// JSON boolean.
    Bool(bool),
    /// JSON string.
    String(String),
    /// JSON integer.
    Int(i64),
    /// JSON unsigned integer.
    UInt(u64),
    /// JSON array.
    Array(Vec<JsonValue>),
    /// JSON object, ordered by key.
    Object(BTreeMap<String, JsonValue>),
}

impl JsonValue {
    /// Build a JSON string value.
    #[must_use]
    pub fn string(value: impl Into<String>) -> Self {
        Self::String(value.into())
    }

    /// Build a JSON object from an iterator of key/value pairs.
    #[must_use]
    pub fn object<I, K>(entries: I) -> Self
    where
        I: IntoIterator<Item = (K, JsonValue)>,
        K: Into<String>,
    {
        Self::Object(
            entries
                .into_iter()
                .map(|(key, value)| (key.into(), value))
                .collect(),
        )
    }

    /// Render the value as canonical JSON.
    #[must_use]
    pub fn to_canonical_string(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        match self {
            Self::Null => out.push_str("null"),
            Self::Bool(true) => out.push_str("true"),
            Self::Bool(false) => out.push_str("false"),
            Self::Int(value) => {
                let _ = write!(out, "{value}");
            }
            Self::UInt(value) => {
                let _ = write!(out, "{value}");
            }
            Self::String(value) => write_string(value, out),
            Self::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    item.write(out);
                }
                out.push(']');
            }
            Self::Object(entries) => {
                out.push('{');
                for (index, (key, value)) in entries.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write_string(key, out);
                    out.push(':');
                    value.write(out);
                }
                out.push('}');
            }
        }
    }
}

fn write_string(value: &str, out: &mut String) {
    out.push('"');
    for character in value.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            control if (control as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", control as u32);
            }
            ascii if (ascii as u32) < 0x80 => out.push(ascii),
            wide => {
                let code = wide as u32;
                if code <= 0xffff {
                    let _ = write!(out, "\\u{code:04x}");
                } else {
                    let adjusted = code - 0x1_0000;
                    let high = 0xd800 + (adjusted >> 10);
                    let low = 0xdc00 + (adjusted & 0x3ff);
                    let _ = write!(out, "\\u{high:04x}\\u{low:04x}");
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_python_dumps_style() {
        let value = JsonValue::object([
            ("b", JsonValue::Int(2)),
            ("a", JsonValue::String("x".to_string())),
            ("c", JsonValue::Array(vec![JsonValue::Bool(true), JsonValue::Null])),
        ]);
        assert_eq!(value.to_canonical_string(), r#"{"a":"x","b":2,"c":[true,null]}"#);
    }

    #[test]
    fn escapes_control_and_non_ascii() {
        let value = JsonValue::String("a\"b\\c\nd\u{00e9}\u{1f600}".to_string());
        assert_eq!(
            value.to_canonical_string(),
            "\"a\\\"b\\\\c\\nd\\u00e9\\ud83d\\ude00\""
        );
    }

    #[test]
    fn empty_containers() {
        assert_eq!(JsonValue::Array(vec![]).to_canonical_string(), "[]");
        assert_eq!(
            JsonValue::Object(BTreeMap::new()).to_canonical_string(),
            "{}"
        );
    }
}
