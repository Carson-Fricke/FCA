//! Minimal non-executable YAML subset loader.
//!
//! Port of `harness.configuration.loader`'s parser: indentation-based
//! mappings and sequences, flow sequences, quoted scalars, and the exact
//! scalar inference (`null`, booleans, integers, exact decimals, strings).
//! This intentionally supports only the YAML subset the repository's
//! configuration files use.

use std::collections::BTreeMap;

use harness_num::PyDecimal;

/// One parsed YAML value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum YamlValue {
    /// A null scalar.
    Null,
    /// A boolean scalar.
    Bool(bool),
    /// An integer scalar.
    Int(i64),
    /// An exact decimal scalar.
    Decimal(PyDecimal),
    /// A string scalar.
    String(String),
    /// A sequence.
    Sequence(Vec<YamlValue>),
    /// A mapping.
    Mapping(BTreeMap<String, YamlValue>),
}

impl YamlValue {
    /// Borrow the value as a mapping.
    #[must_use]
    pub fn as_mapping(&self) -> Option<&BTreeMap<String, YamlValue>> {
        match self {
            Self::Mapping(entries) => Some(entries),
            _ => None,
        }
    }

    /// Borrow the value as a sequence.
    #[must_use]
    pub fn as_sequence(&self) -> Option<&[YamlValue]> {
        match self {
            Self::Sequence(items) => Some(items),
            _ => None,
        }
    }

    /// Borrow the value as a string.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(text) => Some(text),
            _ => None,
        }
    }

    /// The value as an integer when it is an integer scalar.
    #[must_use]
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Self::Int(value) => Some(*value),
            _ => None,
        }
    }

    /// The value as an exact decimal when it is numeric.
    #[must_use]
    pub fn as_decimal(&self) -> Option<PyDecimal> {
        match self {
            Self::Decimal(value) => Some(*value),
            Self::Int(value) => Some(PyDecimal::from_i128(i128::from(*value))),
            _ => None,
        }
    }

    /// The value as a boolean.
    #[must_use]
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }
}

/// A YAML parse failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct YamlError(pub String);

impl std::fmt::Display for YamlError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for YamlError {}

#[derive(Clone, Debug)]
struct YamlLine {
    number: usize,
    indent: usize,
    text: String,
}

/// Remove an unquoted trailing comment.
fn strip_comment(text: &str) -> Result<&str, YamlError> {
    let bytes = text.as_bytes();
    let mut quote: Option<u8> = None;
    let mut bracket_depth: i32 = 0;
    let mut escaped = false;
    let mut index = 0;
    while index < bytes.len() {
        let character = bytes[index];
        if quote == Some(b'"') && escaped {
            escaped = false;
            index += 1;
            continue;
        }
        if quote == Some(b'"') && character == b'\\' {
            escaped = true;
            index += 1;
            continue;
        }
        if character == b'\'' || character == b'"' {
            quote = if quote.is_none() {
                Some(character)
            } else if quote == Some(character) {
                None
            } else {
                quote
            };
            index += 1;
            continue;
        }
        if quote.is_none() {
            if character == b'[' {
                bracket_depth += 1;
            } else if character == b']' {
                bracket_depth -= 1;
            } else if character == b'#'
                && bracket_depth == 0
                && (index == 0 || bytes[index - 1].is_ascii_whitespace())
            {
                return Ok(text[..index].trim_end());
            }
        }
        index += 1;
    }
    if quote.is_some() || bracket_depth != 0 {
        return Err(YamlError(
            "unterminated YAML quote or flow sequence".to_string(),
        ));
    }
    Ok(text.trim_end())
}

fn split_mapping_entry(text: &str) -> Result<(String, String), YamlError> {
    let bytes = text.as_bytes();
    let mut quote: Option<u8> = None;
    let mut bracket_depth: i32 = 0;
    for index in 0..bytes.len() {
        let character = bytes[index];
        if character == b'\'' || character == b'"' {
            quote = if quote.is_none() {
                Some(character)
            } else if quote == Some(character) {
                None
            } else {
                quote
            };
        } else if quote.is_none() {
            if character == b'[' {
                bracket_depth += 1;
            } else if character == b']' {
                bracket_depth -= 1;
            } else if character == b':'
                && bracket_depth == 0
                && (index + 1 == bytes.len() || bytes[index + 1].is_ascii_whitespace())
            {
                let key = text[..index].trim();
                if !key.is_empty() {
                    return Ok((key.to_string(), text[index + 1..].trim().to_string()));
                }
            }
        }
    }
    Err(YamlError(format!(
        "expected a YAML mapping entry: {text:?}"
    )))
}

fn split_flow_values(text: &str) -> Result<Vec<String>, YamlError> {
    let bytes = text.as_bytes();
    let mut values = Vec::new();
    let mut start = 0usize;
    let mut quote: Option<u8> = None;
    let mut depth: i32 = 0;
    for index in 0..bytes.len() {
        let character = bytes[index];
        if character == b'\'' || character == b'"' {
            quote = if quote.is_none() {
                Some(character)
            } else if quote == Some(character) {
                None
            } else {
                quote
            };
        } else if quote.is_none() {
            if character == b'[' {
                depth += 1;
            } else if character == b']' {
                depth -= 1;
            } else if character == b',' && depth == 0 {
                values.push(text[start..index].trim().to_string());
                start = index + 1;
            }
        }
    }
    if quote.is_some() || depth != 0 {
        return Err(YamlError(format!("invalid YAML flow sequence: {text:?}")));
    }
    values.push(text[start..].trim().to_string());
    Ok(values)
}

fn unquote(value: &str) -> String {
    if value.len() >= 2 && value.starts_with('\'') && value.ends_with('\'') {
        return value[1..value.len() - 1].replace("''", "'");
    }
    if value.len() >= 2 && value.starts_with('"') && value.ends_with('"') {
        let inner = &value[1..value.len() - 1];
        return inner
            .replace("\\\"", "\"")
            .replace("\\\\", "\\")
            .replace("\\n", "\n");
    }
    value.to_string()
}

fn is_integer(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut index = 0;
    if bytes[0] == b'+' || bytes[0] == b'-' {
        index = 1;
    }
    if index == bytes.len() {
        return false;
    }
    bytes[index..].iter().all(u8::is_ascii_digit)
}

fn is_decimal(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut index = 0;
    if bytes[0] == b'+' || bytes[0] == b'-' {
        index = 1;
    }
    let mut seen_digit = false;
    let mut seen_point = false;
    while index < bytes.len() {
        let character = bytes[index];
        if character.is_ascii_digit() {
            seen_digit = true;
            index += 1;
        } else if character == b'.' && !seen_point {
            seen_point = true;
            index += 1;
        } else if (character == b'e' || character == b'E') && seen_digit {
            index += 1;
            if index < bytes.len() && (bytes[index] == b'+' || bytes[index] == b'-') {
                index += 1;
            }
            if index >= bytes.len() {
                return false;
            }
            return bytes[index..].iter().all(u8::is_ascii_digit) && seen_point;
        } else {
            return false;
        }
    }
    seen_digit && seen_point
}

fn scalar(value: &str) -> Result<YamlValue, YamlError> {
    if value.is_empty() {
        return Ok(YamlValue::Null);
    }
    let first = value.as_bytes()[0];
    if first == b'\'' || first == b'"' {
        if value.len() < 2 || value.as_bytes()[value.len() - 1] != first {
            return Err(YamlError(format!(
                "unterminated quoted YAML value: {value:?}"
            )));
        }
        return Ok(YamlValue::String(unquote(value)));
    }
    if value.starts_with('[') && value.ends_with(']') {
        let contents = value[1..value.len() - 1].trim();
        if contents.is_empty() {
            return Ok(YamlValue::Sequence(Vec::new()));
        }
        let mut items = Vec::new();
        for entry in split_flow_values(contents)? {
            items.push(scalar(&entry)?);
        }
        return Ok(YamlValue::Sequence(items));
    }
    match value {
        "null" | "Null" | "NULL" | "~" => return Ok(YamlValue::Null),
        "true" | "True" | "TRUE" => return Ok(YamlValue::Bool(true)),
        "false" | "False" | "FALSE" => return Ok(YamlValue::Bool(false)),
        _ => {}
    }
    if is_integer(value) {
        if let Ok(parsed) = value.parse::<i64>() {
            return Ok(YamlValue::Int(parsed));
        }
    }
    if is_decimal(value) {
        if let Ok(parsed) = value.parse::<PyDecimal>() {
            return Ok(YamlValue::Decimal(parsed));
        }
    }
    if value.starts_with('&')
        || value.starts_with('*')
        || value.starts_with('!')
        || value.starts_with('{')
    {
        return Err(YamlError(format!("unsupported YAML value: {value:?}")));
    }
    Ok(YamlValue::String(value.to_string()))
}

fn clean_lines(text: &str) -> Result<Vec<YamlLine>, YamlError> {
    let mut lines = Vec::new();
    for (number, raw) in text.lines().enumerate() {
        let trimmed = raw.trim_start_matches(' ');
        let indent_text = &raw[..raw.len() - trimmed.len()];
        if indent_text.contains('\t') {
            return Err(YamlError(format!(
                "YAML line {} uses tabs for indentation",
                number + 1
            )));
        }
        let content = strip_comment(trimmed)?;
        if !content.is_empty() {
            lines.push(YamlLine {
                number: number + 1,
                indent: indent_text.len(),
                text: content.to_string(),
            });
        }
    }
    Ok(lines)
}

fn parse_block(
    lines: &[YamlLine],
    index: usize,
    indent: usize,
) -> Result<(YamlValue, usize), YamlError> {
    if index >= lines.len() || lines[index].indent != indent {
        return Err(YamlError("invalid YAML indentation".to_string()));
    }
    let sequence = lines[index].text == "-" || lines[index].text.starts_with("- ");
    if sequence {
        let mut result = Vec::new();
        let mut cursor = index;
        while cursor < lines.len() && lines[cursor].indent == indent {
            let line = &lines[cursor];
            if !(line.text == "-" || line.text.starts_with("- ")) {
                break;
            }
            let item = line.text[1..].trim();
            if !item.is_empty() {
                result.push(scalar(item)?);
                cursor += 1;
            } else if cursor + 1 < lines.len() && lines[cursor + 1].indent > indent {
                let (parsed, next) = parse_block(lines, cursor + 1, lines[cursor + 1].indent)?;
                result.push(parsed);
                cursor = next;
            } else {
                return Err(YamlError(format!(
                    "YAML line {} has an empty sequence item",
                    line.number
                )));
            }
        }
        return Ok((YamlValue::Sequence(result), cursor));
    }
    let mut result: BTreeMap<String, YamlValue> = BTreeMap::new();
    let mut cursor = index;
    while cursor < lines.len() && lines[cursor].indent == indent {
        let line = &lines[cursor];
        if line.text == "-" || line.text.starts_with("- ") {
            break;
        }
        let (raw_key, raw_value) = split_mapping_entry(&line.text)?;
        let key = unquote(&raw_key);
        if key.is_empty() || key.chars().any(char::is_whitespace) {
            return Err(YamlError(format!(
                "invalid YAML key on line {}: {raw_key:?}",
                line.number
            )));
        }
        if result.contains_key(&key) {
            return Err(YamlError(format!(
                "duplicate YAML key {key:?} on line {}",
                line.number
            )));
        }
        cursor += 1;
        let parsed = if !raw_value.is_empty() {
            scalar(&raw_value)?
        } else if cursor < lines.len() && lines[cursor].indent > indent {
            let (value, next) = parse_block(lines, cursor, lines[cursor].indent)?;
            cursor = next;
            value
        } else {
            YamlValue::Null
        };
        result.insert(key, parsed);
    }
    Ok((YamlValue::Mapping(result), cursor))
}

/// Load the repository's non-executable YAML subset from text.
///
/// # Errors
///
/// Returns [`YamlError`] when the document is malformed or unsupported.
pub fn load_yaml(text: &str) -> Result<YamlValue, YamlError> {
    let lines = clean_lines(text)?;
    if lines.is_empty() {
        return Err(YamlError("configuration file is empty".to_string()));
    }
    let (parsed, index) = parse_block(&lines, 0, lines[0].indent)?;
    if index != lines.len() {
        return Err(YamlError(format!(
            "invalid YAML near line {}",
            lines[index].number
        )));
    }
    if !matches!(parsed, YamlValue::Mapping(_)) {
        return Err(YamlError(
            "configuration root must be a mapping".to_string(),
        ));
    }
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_backtest_subset() {
        let text = "shell: backtest\nseed: 1729\ninitial_cash: 100000.00\ndata:\n  symbols: [SPY]\n  bar_interval: 1min\nrisk_pipeline:\n  on_reject: reject_order\n  rules:\n    -\n      rule_id: max-order-notional\n      parameters:\n        limit: 10000.00\n";
        let value = load_yaml(text).expect("parse");
        let map = value.as_mapping().expect("mapping");
        assert_eq!(map["shell"].as_str(), Some("backtest"));
        assert_eq!(map["seed"].as_int(), Some(1729));
        assert_eq!(
            map["initial_cash"].as_decimal().map(|v| v.to_string()),
            Some("100000.00".to_string())
        );
        let data = map["data"].as_mapping().expect("data");
        assert_eq!(data["bar_interval"].as_str(), Some("1min"));
        let rules = map["risk_pipeline"].as_mapping().expect("rp")["rules"]
            .as_sequence()
            .expect("rules");
        assert_eq!(rules.len(), 1);
        assert_eq!(
            rules[0].as_mapping().expect("rule")["rule_id"].as_str(),
            Some("max-order-notional")
        );
    }

    #[test]
    fn strips_comments_and_rejects_duplicates() {
        assert_eq!(
            load_yaml("a: 1 # comment\n")
                .expect("ok")
                .as_mapping()
                .unwrap()["a"]
                .as_int(),
            Some(1)
        );
        assert!(load_yaml("a: 1\na: 2\n").is_err());
    }
}
