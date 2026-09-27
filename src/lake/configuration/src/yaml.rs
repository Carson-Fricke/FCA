//! Strict YAML-subset parser for the committed market-lake files.
//!
//! Port of `lake.configuration.loader`: comments stripped quote/bracket aware,
//! duplicate keys rejected, tabs rejected, anchors/aliases/tags/flow maps
//! rejected, scalars null/true/false/int/float/string, double-quoted JSON
//! decoding, single-quoted `''` escaping, `- ` block sequences, and `[a, b]`
//! flow sequences. Secret-bearing keys are rejected outright.

use std::path::Path;

use serde_json::{Map, Value};

use crate::error::ConfigError;

#[derive(Clone, Debug)]
struct YamlLine {
    number: usize,
    indent: usize,
    text: String,
}

/// Parse the strict YAML subset into a JSON value.
pub fn parse_yaml(text: &str) -> Result<Value, ConfigError> {
    let lines = clean_lines(text)?;
    if lines.is_empty() {
        return Err(ConfigError::new("configuration file is empty"));
    }
    let (parsed, index) = parse_block(&lines, 0, lines[0].indent)?;
    if index != lines.len() {
        return Err(ConfigError::new(format!(
            "invalid YAML near line {}",
            lines[index].number
        )));
    }
    Ok(parsed)
}

/// Load a strict YAML mapping file, requiring a mapping root.
pub fn load_yaml_mapping(path: &Path) -> Result<Value, ConfigError> {
    let text = std::fs::read_to_string(path)?;
    let lines = clean_lines(&text)?;
    if lines.is_empty() {
        return Err(ConfigError::new(format!(
            "configuration file is empty: {}",
            path.display()
        )));
    }
    let (parsed, index) = parse_block(&lines, 0, lines[0].indent)?;
    if index != lines.len() {
        return Err(ConfigError::new(format!(
            "invalid YAML near line {}",
            lines[index].number
        )));
    }
    if !parsed.is_object() {
        return Err(ConfigError::new(format!(
            "configuration root must be a mapping: {}",
            path.display()
        )));
    }
    Ok(parsed)
}

fn strip_comment(text: &str) -> Result<String, ConfigError> {
    let characters: Vec<char> = text.chars().collect();
    let mut quote: Option<char> = None;
    let mut bracket_depth: i64 = 0;
    let mut escaped = false;
    for (index, character) in characters.iter().enumerate() {
        if quote == Some('"') && escaped {
            escaped = false;
            continue;
        }
        if quote == Some('"') && *character == '\\' {
            escaped = true;
            continue;
        }
        if *character == '\'' || *character == '"' {
            if quote.is_none() {
                quote = Some(*character);
            } else if quote == Some(*character) {
                quote = None;
            }
            continue;
        }
        if quote.is_none() {
            if "([{".contains(*character) {
                bracket_depth += 1;
            } else if "])}".contains(*character) {
                bracket_depth -= 1;
            } else if *character == '#'
                && bracket_depth == 0
                && (index == 0 || characters[index - 1].is_whitespace())
            {
                let prefix: String = characters[..index].iter().collect();
                return Ok(prefix.trim_end().to_string());
            }
        }
    }
    if quote.is_some() {
        return Err(ConfigError::new("unterminated quoted YAML value"));
    }
    if bracket_depth != 0 {
        return Err(ConfigError::new("unclosed YAML flow collection"));
    }
    Ok(text.trim_end().to_string())
}

fn split_mapping_entry(text: &str) -> Result<(String, String), ConfigError> {
    let characters: Vec<char> = text.chars().collect();
    let mut quote: Option<char> = None;
    let mut bracket_depth: i64 = 0;
    let mut escaped = false;
    for (index, character) in characters.iter().enumerate() {
        if quote == Some('"') && escaped {
            escaped = false;
            continue;
        }
        if quote == Some('"') && *character == '\\' {
            escaped = true;
            continue;
        }
        if *character == '\'' || *character == '"' {
            if quote.is_none() {
                quote = Some(*character);
            } else if quote == Some(*character) {
                quote = None;
            }
            continue;
        }
        if quote.is_none() {
            if "([{".contains(*character) {
                bracket_depth += 1;
            } else if "])}".contains(*character) {
                bracket_depth -= 1;
            } else if *character == ':' && bracket_depth == 0 {
                let at_end = index + 1 == characters.len();
                if at_end || characters[index + 1].is_whitespace() {
                    let key: String = characters[..index].iter().collect();
                    let key = key.trim().to_string();
                    if key.is_empty() {
                        break;
                    }
                    let rest: String = characters[index + 1..].iter().collect();
                    return Ok((key, rest.trim().to_string()));
                }
            }
        }
    }
    Err(ConfigError::new(format!(
        "expected a mapping entry: {text:?}"
    )))
}

fn split_flow_values(text: &str) -> Result<Vec<String>, ConfigError> {
    let characters: Vec<char> = text.chars().collect();
    let mut values = Vec::new();
    let mut start = 0usize;
    let mut quote: Option<char> = None;
    let mut bracket_depth: i64 = 0;
    let mut escaped = false;
    for (index, character) in characters.iter().enumerate() {
        if quote == Some('"') && escaped {
            escaped = false;
            continue;
        }
        if quote == Some('"') && *character == '\\' {
            escaped = true;
            continue;
        }
        if *character == '\'' || *character == '"' {
            if quote.is_none() {
                quote = Some(*character);
            } else if quote == Some(*character) {
                quote = None;
            }
        } else if quote.is_none() {
            if *character == '[' {
                bracket_depth += 1;
            } else if *character == ']' {
                bracket_depth -= 1;
            } else if *character == ',' && bracket_depth == 0 {
                values.push(
                    characters[start..index]
                        .iter()
                        .collect::<String>()
                        .trim()
                        .to_string(),
                );
                start = index + 1;
            }
        }
    }
    if quote.is_some() || bracket_depth != 0 {
        return Err(ConfigError::new(format!(
            "invalid flow collection: {text:?}"
        )));
    }
    values.push(
        characters[start..]
            .iter()
            .collect::<String>()
            .trim()
            .to_string(),
    );
    Ok(values)
}

fn unquote(value: &str) -> Result<String, ConfigError> {
    if value.starts_with('\'') && value.ends_with('\'') && value.len() >= 2 {
        return Ok(value[1..value.len() - 1].replace("''", "'"));
    }
    if value.starts_with('"') && value.ends_with('"') && value.len() >= 2 {
        let parsed: Value = serde_json::from_str(value).map_err(|_| {
            ConfigError::new(format!("invalid double-quoted YAML value: {value:?}"))
        })?;
        return match parsed {
            Value::String(text) => Ok(text),
            _ => Err(ConfigError::new(format!(
                "expected a string value: {value:?}"
            ))),
        };
    }
    Ok(value.to_string())
}

fn scalar(value: &str) -> Result<Value, ConfigError> {
    if value.is_empty() {
        return Ok(Value::Null);
    }
    let first = value.chars().next().expect("non-empty");
    if first == '\'' || first == '"' {
        if value.len() < 2 || !value.ends_with(first) {
            return Err(ConfigError::new(format!(
                "unterminated quoted YAML value: {value:?}"
            )));
        }
        return Ok(Value::String(unquote(value)?));
    }
    if value.starts_with('[') && value.ends_with(']') {
        let contents = value[1..value.len() - 1].trim();
        if contents.is_empty() {
            return Ok(Value::Array(Vec::new()));
        }
        let mut items = Vec::new();
        for item in split_flow_values(contents)? {
            items.push(scalar(&item)?);
        }
        return Ok(Value::Array(items));
    }
    match value {
        "null" | "Null" | "NULL" | "~" => return Ok(Value::Null),
        "true" | "True" | "TRUE" => return Ok(Value::Bool(true)),
        "false" | "False" | "FALSE" => return Ok(Value::Bool(false)),
        _ => {}
    }
    if is_integer(value) {
        if let Ok(number) = value.parse::<i64>() {
            return Ok(Value::Number(number.into()));
        }
        return Ok(Value::String(value.to_string()));
    }
    if is_decimal(value) {
        if let Ok(number) = value.parse::<f64>() {
            if let Some(number) = serde_json::Number::from_f64(number) {
                return Ok(Value::Number(number));
            }
        }
        return Ok(Value::String(value.to_string()));
    }
    if value.starts_with(['&', '*', '!', '{']) {
        return Err(ConfigError::new(format!(
            "unsupported YAML value: {value:?}"
        )));
    }
    Ok(Value::String(value.to_string()))
}

fn is_integer(value: &str) -> bool {
    let body = value.strip_prefix(['+', '-']).unwrap_or(value);
    !body.is_empty() && body.bytes().all(|byte| byte.is_ascii_digit())
}

fn is_decimal(value: &str) -> bool {
    let body = value.strip_prefix(['+', '-']).unwrap_or(value);
    let Some((whole, fraction)) = body.split_once('.') else {
        return false;
    };
    let whole_ok = whole.is_empty() || whole.bytes().all(|byte| byte.is_ascii_digit());
    let fraction_ok = fraction.is_empty() || fraction.bytes().all(|byte| byte.is_ascii_digit());
    whole_ok && fraction_ok && !(whole.is_empty() && fraction.is_empty())
}

fn is_secret_key(key: &str) -> bool {
    let normalized = key.to_ascii_lowercase().replace('-', "_");
    const PATTERNS: &[&str] = &[
        "access_key",
        "accesskey",
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "bearer",
        "client_secret",
        "clientsecret",
        "connection_string",
        "connectionstring",
        "credential",
        "password",
        "private_key",
        "privatekey",
        "secret",
        "token",
    ];
    PATTERNS.iter().any(|pattern| {
        normalized == *pattern
            || normalized.starts_with(&format!("{pattern}_"))
            || normalized.ends_with(&format!("_{pattern}"))
            || normalized.contains(&format!("_{pattern}_"))
    })
}

fn key(value: &str) -> Result<String, ConfigError> {
    let parsed = unquote(value)?;
    if parsed.is_empty() || parsed.chars().any(char::is_whitespace) {
        return Err(ConfigError::new(format!(
            "invalid YAML mapping key: {value:?}"
        )));
    }
    if is_secret_key(&parsed) {
        return Err(ConfigError::new(format!(
            "secret-bearing YAML key {parsed:?} is not allowed; use the environment or a Key Vault reference"
        )));
    }
    Ok(parsed)
}

fn clean_lines(text: &str) -> Result<Vec<YamlLine>, ConfigError> {
    let mut lines = Vec::new();
    for (number, raw_line) in text.lines().enumerate() {
        let number = number + 1;
        let trimmed_start = raw_line.trim_start();
        let leading = &raw_line[..raw_line.len() - trimmed_start.len()];
        if leading.contains('\t') {
            return Err(ConfigError::new(format!(
                "YAML line {number} uses tabs for indentation"
            )));
        }
        let content = strip_comment(trimmed_start)?;
        if content.is_empty() {
            continue;
        }
        lines.push(YamlLine {
            number,
            indent: leading.len(),
            text: content,
        });
    }
    Ok(lines)
}

fn is_sequence_item(text: &str) -> bool {
    text == "-" || text.starts_with("- ")
}

fn parse_block(
    lines: &[YamlLine],
    index: usize,
    indent: usize,
) -> Result<(Value, usize), ConfigError> {
    if index >= lines.len() || lines[index].indent != indent {
        return Err(ConfigError::new("invalid YAML indentation"));
    }
    if is_sequence_item(&lines[index].text) {
        let mut result = Vec::new();
        let mut cursor = index;
        while cursor < lines.len() && lines[cursor].indent == indent {
            let line = &lines[cursor];
            if !is_sequence_item(&line.text) {
                break;
            }
            let item = line.text[1..].trim().to_string();
            if item.is_empty() {
                if cursor + 1 >= lines.len() || lines[cursor + 1].indent <= indent {
                    return Err(ConfigError::new(format!(
                        "YAML line {} has an empty sequence item",
                        line.number
                    )));
                }
                let (value, next) = parse_block(lines, cursor + 1, lines[cursor + 1].indent)?;
                result.push(value);
                cursor = next;
            } else {
                result.push(scalar(&item)?);
                cursor += 1;
                if cursor < lines.len() && lines[cursor].indent > indent {
                    return Err(ConfigError::new(format!(
                        "unexpected YAML indentation on line {}",
                        lines[cursor].number
                    )));
                }
            }
        }
        return Ok((Value::Array(result), cursor));
    }

    let mut result = Map::new();
    let mut cursor = index;
    while cursor < lines.len() && lines[cursor].indent == indent {
        let line = &lines[cursor];
        if is_sequence_item(&line.text) {
            break;
        }
        let (raw_key, raw_value) = split_mapping_entry(&line.text)?;
        let parsed_key = key(&raw_key)?;
        if result.contains_key(&parsed_key) {
            return Err(ConfigError::new(format!(
                "duplicate YAML key {parsed_key:?} on line {}",
                line.number
            )));
        }
        cursor += 1;
        let value = if !raw_value.is_empty() {
            let value = scalar(&raw_value)?;
            if cursor < lines.len() && lines[cursor].indent > indent {
                return Err(ConfigError::new(format!(
                    "unexpected YAML indentation on line {}",
                    lines[cursor].number
                )));
            }
            value
        } else if cursor < lines.len() && lines[cursor].indent > indent {
            let (value, next) = parse_block(lines, cursor, lines[cursor].indent)?;
            cursor = next;
            value
        } else {
            Value::Null
        };
        result.insert(parsed_key, value);
    }
    Ok((Value::Object(result), cursor))
}

#[cfg(test)]
#[path = "../tests/unit/yaml.rs"]
mod tests;
