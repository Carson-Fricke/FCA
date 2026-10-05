//! Deterministic YAML-subset parser.
//!
//! A faithful port of `harness.configuration.loader`: comments, quotes, flow
//! sequences, block mappings and sequences, and exact-decimal scalars. It
//! never executes configuration and never consults the environment.

use std::collections::BTreeMap;

use crate::ConfigError;

/// One parsed YAML value from the supported subset.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum YamlValue {
    /// `null`, `~`, or an empty value.
    Null,
    /// A boolean scalar.
    Bool(bool),
    /// An integer scalar.
    Int(i64),
    /// An exact decimal scalar, retained as its source text.
    Decimal(String),
    /// A string scalar.
    Str(String),
    /// A block or flow sequence.
    Seq(Vec<YamlValue>),
    /// A mapping.
    Map(BTreeMap<String, YamlValue>),
}

#[derive(Clone)]
struct YamlLine {
    number: usize,
    indent: usize,
    text: String,
}

fn is_integer(text: &str) -> bool {
    let mut chars = text.chars().peekable();
    if matches!(chars.peek(), Some('+') | Some('-')) {
        chars.next();
    }
    let mut saw_digit = false;
    for character in chars {
        if character.is_ascii_digit() {
            saw_digit = true;
        } else {
            return false;
        }
    }
    saw_digit
}

fn is_decimal(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut index = 0usize;
    if matches!(bytes.first(), Some(b'+') | Some(b'-')) {
        index += 1;
    }
    let mut integer_digits = 0usize;
    while index < bytes.len() && bytes[index].is_ascii_digit() {
        integer_digits += 1;
        index += 1;
    }
    let mut fraction_digits = 0usize;
    if index < bytes.len() && bytes[index] == b'.' {
        index += 1;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            fraction_digits += 1;
            index += 1;
        }
        if integer_digits == 0 && fraction_digits == 0 {
            return false;
        }
    } else {
        return false;
    }
    if index < bytes.len() && (bytes[index] == b'e' || bytes[index] == b'E') {
        index += 1;
        if index < bytes.len() && (bytes[index] == b'+' || bytes[index] == b'-') {
            index += 1;
        }
        let mut exponent_digits = 0usize;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            exponent_digits += 1;
            index += 1;
        }
        if exponent_digits == 0 {
            return false;
        }
    }
    index == bytes.len()
}

fn strip_comment(text: &str) -> Result<String, ConfigError> {
    let chars: Vec<char> = text.chars().collect();
    let mut quote: Option<char> = None;
    let mut bracket_depth = 0i32;
    let mut escaped = false;
    for (index, character) in chars.iter().enumerate() {
        if quote == Some('"') && escaped {
            escaped = false;
            continue;
        }
        if quote == Some('"') && *character == '\\' {
            escaped = true;
            continue;
        }
        if *character == '\'' || *character == '"' {
            quote = match quote {
                None => Some(*character),
                Some(active) if active == *character => None,
                Some(active) => Some(active),
            };
            continue;
        }
        if quote.is_none() {
            match character {
                '[' => bracket_depth += 1,
                ']' => bracket_depth -= 1,
                '#' if bracket_depth == 0 => {
                    let previous_is_space = index == 0 || chars[index - 1].is_whitespace();
                    if previous_is_space {
                        let prefix: String = chars[..index].iter().collect();
                        return Ok(prefix.trim_end().to_string());
                    }
                }
                _ => {}
            }
        }
    }
    if quote.is_some() || bracket_depth != 0 {
        return Err(ConfigError(
            "unterminated YAML quote or flow sequence".to_string(),
        ));
    }
    Ok(text.trim_end().to_string())
}

fn split_mapping_entry(text: &str) -> Result<(String, String), ConfigError> {
    let chars: Vec<char> = text.chars().collect();
    let mut quote: Option<char> = None;
    let mut bracket_depth = 0i32;
    for (index, character) in chars.iter().enumerate() {
        if *character == '\'' || *character == '"' {
            quote = match quote {
                None => Some(*character),
                Some(active) if active == *character => None,
                Some(active) => Some(active),
            };
        } else if quote.is_none() {
            match character {
                '[' => bracket_depth += 1,
                ']' => bracket_depth -= 1,
                ':' if bracket_depth == 0 => {
                    let next_is_space =
                        index + 1 == chars.len() || chars[index + 1].is_whitespace();
                    if next_is_space {
                        let key: String = chars[..index].iter().collect();
                        let key = key.trim().to_string();
                        if !key.is_empty() {
                            let value: String = chars[index + 1..].iter().collect();
                            return Ok((key, value.trim().to_string()));
                        }
                    }
                }
                _ => {}
            }
        }
    }
    Err(ConfigError(format!(
        "expected a YAML mapping entry: {text:?}"
    )))
}

fn split_flow_values(text: &str) -> Result<Vec<String>, ConfigError> {
    let chars: Vec<char> = text.chars().collect();
    let mut values = Vec::new();
    let mut start = 0usize;
    let mut quote: Option<char> = None;
    let mut depth = 0i32;
    for (index, character) in chars.iter().enumerate() {
        if *character == '\'' || *character == '"' {
            quote = match quote {
                None => Some(*character),
                Some(active) if active == *character => None,
                Some(active) => Some(active),
            };
        } else if quote.is_none() {
            match character {
                '[' => depth += 1,
                ']' => depth -= 1,
                ',' if depth == 0 => {
                    values.push(
                        chars[start..index]
                            .iter()
                            .collect::<String>()
                            .trim()
                            .to_string(),
                    );
                    start = index + 1;
                }
                _ => {}
            }
        }
    }
    if quote.is_some() || depth != 0 {
        return Err(ConfigError(format!("invalid YAML flow sequence: {text:?}")));
    }
    values.push(chars[start..].iter().collect::<String>().trim().to_string());
    Ok(values)
}

fn unquote(value: &str) -> Result<String, ConfigError> {
    if value.starts_with('\'') && value.ends_with('\'') && value.len() >= 2 {
        return Ok(value[1..value.len() - 1].replace("''", "'"));
    }
    if value.starts_with('"') && value.ends_with('"') && value.len() >= 2 {
        let parsed: String = serde_json::from_str(value)
            .map_err(|_| ConfigError(format!("expected a YAML string: {value:?}")))?;
        return Ok(parsed);
    }
    Ok(value.to_string())
}

fn scalar(value: &str) -> Result<YamlValue, ConfigError> {
    if value.is_empty() {
        return Ok(YamlValue::Null);
    }
    let first = value.chars().next().unwrap_or(' ');
    if first == '\'' || first == '"' {
        let last = value.chars().last().unwrap_or(' ');
        if value.len() < 2 || last != first {
            return Err(ConfigError(format!(
                "unterminated quoted YAML value: {value:?}"
            )));
        }
        return Ok(YamlValue::Str(unquote(value)?));
    }
    if value.starts_with('[') && value.ends_with(']') {
        let contents = value[1..value.len() - 1].trim();
        if contents.is_empty() {
            return Ok(YamlValue::Seq(Vec::new()));
        }
        let mut items = Vec::new();
        for entry in split_flow_values(contents)? {
            items.push(scalar(&entry)?);
        }
        return Ok(YamlValue::Seq(items));
    }
    match value {
        "null" | "Null" | "NULL" | "~" => return Ok(YamlValue::Null),
        "true" | "True" | "TRUE" => return Ok(YamlValue::Bool(true)),
        "false" | "False" | "FALSE" => return Ok(YamlValue::Bool(false)),
        _ => {}
    }
    if is_integer(value) {
        return value
            .parse::<i64>()
            .map(YamlValue::Int)
            .map_err(|error| ConfigError(format!("invalid YAML integer: {value:?}: {error}")));
    }
    if is_decimal(value) {
        return Ok(YamlValue::Decimal(value.to_string()));
    }
    if value.starts_with('&')
        || value.starts_with('*')
        || value.starts_with('!')
        || value.starts_with('{')
    {
        return Err(ConfigError(format!("unsupported YAML value: {value:?}")));
    }
    Ok(YamlValue::Str(value.to_string()))
}

fn clean_lines(text: &str) -> Result<Vec<YamlLine>, ConfigError> {
    let mut lines = Vec::new();
    for (index, raw_line) in text.split('\n').enumerate() {
        let raw_line = raw_line.strip_suffix('\r').unwrap_or(raw_line);
        let trimmed = raw_line.trim_start();
        let indent_text = &raw_line[..raw_line.len() - trimmed.len()];
        if indent_text.contains('\t') {
            return Err(ConfigError(format!(
                "YAML line {} uses tabs for indentation",
                index + 1
            )));
        }
        let content = strip_comment(raw_line.trim_start_matches(' '))?;
        if !content.is_empty() {
            lines.push(YamlLine {
                number: index + 1,
                indent: indent_text.len(),
                text: content,
            });
        }
    }
    Ok(lines)
}

fn is_sequence(text: &str) -> bool {
    text == "-" || text.starts_with("- ")
}

fn parse_block(
    lines: &[YamlLine],
    mut index: usize,
    indent: usize,
) -> Result<(YamlValue, usize), ConfigError> {
    if index >= lines.len() || lines[index].indent != indent {
        return Err(ConfigError("invalid YAML indentation".to_string()));
    }
    if is_sequence(&lines[index].text) {
        let mut result = Vec::new();
        while index < lines.len() && lines[index].indent == indent {
            let line_text = lines[index].text.clone();
            if !is_sequence(&line_text) {
                break;
            }
            let item = line_text[1..].trim().to_string();
            if !item.is_empty() {
                if split_mapping_entry(&item).is_ok() && !item.starts_with(['\'', '"', '[']) {
                    // Expand '- key: value' to a regular mapping block, preserving
                    // the following indented fields and the shared duplicate-key checks.
                    let mut next = index + 1;
                    while next < lines.len() && lines[next].indent > indent {
                        next += 1;
                    }
                    let child_indent = indent + 2;
                    let mut expanded = vec![YamlLine {
                        number: lines[index].number,
                        indent: child_indent,
                        text: item,
                    }];
                    expanded.extend_from_slice(&lines[index + 1..next]);
                    let (parsed, consumed) = parse_block(&expanded, 0, child_indent)?;
                    if consumed != expanded.len() {
                        return Err(ConfigError(format!(
                            "invalid YAML near line {}",
                            expanded[consumed].number
                        )));
                    }
                    result.push(parsed);
                    index = next;
                } else {
                    result.push(scalar(&item)?);
                    index += 1;
                }
            } else if index + 1 < lines.len() && lines[index + 1].indent > indent {
                let child_indent = lines[index + 1].indent;
                let (parsed, next) = parse_block(lines, index + 1, child_indent)?;
                result.push(parsed);
                index = next;
            } else {
                return Err(ConfigError(format!(
                    "YAML line {} has an empty sequence item",
                    lines[index].number
                )));
            }
        }
        return Ok((YamlValue::Seq(result), index));
    }

    let mut result_map: BTreeMap<String, YamlValue> = BTreeMap::new();
    while index < lines.len() && lines[index].indent == indent {
        let line_text = lines[index].text.clone();
        if is_sequence(&line_text) {
            break;
        }
        let line_number = lines[index].number;
        let (raw_key, raw_value) = split_mapping_entry(&line_text)?;
        let key = unquote(&raw_key)?;
        if key.is_empty() || key.chars().any(char::is_whitespace) {
            return Err(ConfigError(format!(
                "invalid YAML key on line {line_number}: {raw_key:?}"
            )));
        }
        if result_map.contains_key(&key) {
            return Err(ConfigError(format!(
                "duplicate YAML key {key:?} on line {line_number}"
            )));
        }
        index += 1;
        let parsed = if !raw_value.is_empty() {
            scalar(&raw_value)?
        } else if index < lines.len() && lines[index].indent > indent {
            let child_indent = lines[index].indent;
            let (parsed, next) = parse_block(lines, index, child_indent)?;
            index = next;
            parsed
        } else {
            YamlValue::Null
        };
        result_map.insert(key, parsed);
    }
    Ok((YamlValue::Map(result_map), index))
}

/// Load the non-executable YAML subset used by harness configuration.
///
/// # Errors
///
/// Returns [`ConfigError`] for unreadable, empty, or malformed documents.
pub fn load_yaml_mapping(
    path: &std::path::Path,
) -> Result<BTreeMap<String, YamlValue>, ConfigError> {
    let text = std::fs::read_to_string(path)
        .map_err(|error| ConfigError(format!("configuration could not be loaded: {error}")))?;
    let lines = clean_lines(&text)?;
    if lines.is_empty() {
        return Err(ConfigError(format!(
            "configuration file is empty: {}",
            path.display()
        )));
    }
    let first_indent = lines[0].indent;
    let (parsed, index) = parse_block(&lines, 0, first_indent)?;
    if index != lines.len() {
        return Err(ConfigError(format!(
            "invalid YAML near line {}",
            lines[index].number
        )));
    }
    match parsed {
        YamlValue::Map(mapping) => Ok(mapping),
        _ => Err(ConfigError(format!(
            "configuration root must be a mapping: {}",
            path.display()
        ))),
    }
}

/// Parse the YAML subset from source text.
///
/// # Errors
///
/// Returns [`ConfigError`] for empty or malformed documents.
pub fn parse_yaml_mapping(text: &str) -> Result<BTreeMap<String, YamlValue>, ConfigError> {
    let lines = clean_lines(text)?;
    if lines.is_empty() {
        return Err(ConfigError("configuration file is empty".to_string()));
    }
    let first_indent = lines[0].indent;
    let (parsed, index) = parse_block(&lines, 0, first_indent)?;
    if index != lines.len() {
        return Err(ConfigError(format!(
            "invalid YAML near line {}",
            lines[index].number
        )));
    }
    match parsed {
        YamlValue::Map(mapping) => Ok(mapping),
        _ => Err(ConfigError(
            "configuration root must be a mapping".to_string(),
        )),
    }
}
