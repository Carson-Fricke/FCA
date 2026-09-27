//! SEC EDGAR XBRL company facts parsing.
//!
//! Ports `lake.reference.edgar`. Facts are one archive row per XBRL fact
//! entry: `companyfacts` JSON expands to all taxonomy, tag, and unit entries
//! for one CIK; `companyconcept` JSON expands to the single taxonomy and tag
//! it names. The `filed` date is the point-in-time knowledge key: a fact
//! filed after an as-of instant must stay invisible to as-of queries,
//! including restated values of the same XBRL tag and period.
//!
//! The nightly `companyfacts.zip` bulk archive reader is not ported here; the
//! incremental per-CIK `companyfacts` fetch is the ported ingest surface.

use std::cmp::Ordering;

use lake_contracts::{Date, Decimal18};
use serde_json::Value;

use crate::error::ReferenceError;
use crate::partitions::is_non_finite_decimal;

/// One XBRL fact archive row, keyed by its full identity.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct XbrlFact {
    /// The SEC CIK of the filer.
    pub cik: u64,
    /// The XBRL taxonomy (`us-gaap`, `dei`, ...).
    pub taxonomy: String,
    /// The XBRL tag within the taxonomy.
    pub tag: String,
    /// The unit of measure (`USD`, `shares`, ...).
    pub unit: String,
    /// The period the fact covers.
    pub period_end: Date,
    /// The fiscal year, when the filing declared one.
    pub fy: Option<i32>,
    /// The fiscal period, when the filing declared one.
    pub fp: Option<String>,
    /// The form type of the filing.
    pub form: String,
    /// The accession number of the filing.
    pub accn: String,
    /// The knowledge date: when the fact became public.
    pub filed: Date,
    /// The SEC frame label, when present.
    pub frame: Option<String>,
    /// The exact fact value at lake decimal scale.
    pub val: Decimal18,
}

/// Expand a companyfacts document into sorted, deduplicated facts.
pub fn parse_companyfacts(document: &Value) -> Result<Vec<XbrlFact>, ReferenceError> {
    let cik = cik_of(document)?;
    let facts_by_taxonomy = mapping_of(document, "facts", "companyfacts")?;
    let mut facts: Vec<XbrlFact> = Vec::new();
    for (taxonomy, tags) in facts_by_taxonomy {
        let Some(tags) = tags.as_object() else {
            return Err(ReferenceError::edgar(format!(
                "companyfacts taxonomy {taxonomy:?} must be an object"
            )));
        };
        for (tag, node) in tags {
            facts.extend(taxonomy_facts(cik, taxonomy, tag, node)?);
        }
    }
    Ok(archive_order(facts))
}

/// Parse a companyfacts JSON payload into sorted, deduplicated facts.
pub fn parse_companyfacts_bytes(payload: &[u8]) -> Result<Vec<XbrlFact>, ReferenceError> {
    parse_companyfacts(&document(payload)?)
}

/// Expand a companyconcept document into sorted, deduplicated facts.
pub fn parse_companyconcept(document: &Value) -> Result<Vec<XbrlFact>, ReferenceError> {
    let cik = cik_of(document)?;
    let taxonomy = text_of(document, "taxonomy", "companyconcept")?;
    let tag = text_of(document, "tag", "companyconcept")?;
    let units = mapping_of(document, "units", "companyconcept")?;
    let mut facts: Vec<XbrlFact> = Vec::new();
    for (unit_name, node) in units {
        facts.extend(unit_facts(cik, &taxonomy, &tag, unit_name, node)?);
    }
    Ok(archive_order(facts))
}

/// Collapse exact-duplicate rows while preserving archive order.
pub fn deduplicate_facts(facts: &[XbrlFact]) -> Vec<XbrlFact> {
    archive_order(facts.to_vec())
}

/// Sort and deduplicate facts into the stable archive order.
fn archive_order(mut facts: Vec<XbrlFact>) -> Vec<XbrlFact> {
    facts.sort_by(compare_archive_key);
    facts.dedup();
    facts
}

/// Compare facts by the Python `_fact_key` archive order: `(cik, taxonomy,
/// tag, unit, period_end, filed, accn, fy, fp, form, frame, val)` where a
/// missing `fy`, `fp`, or `frame` participates as zero or the empty string.
fn compare_archive_key(left: &XbrlFact, right: &XbrlFact) -> Ordering {
    left.cik
        .cmp(&right.cik)
        .then_with(|| left.taxonomy.cmp(&right.taxonomy))
        .then_with(|| left.tag.cmp(&right.tag))
        .then_with(|| left.unit.cmp(&right.unit))
        .then_with(|| left.period_end.cmp(&right.period_end))
        .then_with(|| left.filed.cmp(&right.filed))
        .then_with(|| left.accn.cmp(&right.accn))
        .then_with(|| left.fy.unwrap_or(0).cmp(&right.fy.unwrap_or(0)))
        .then_with(|| {
            left.fp
                .as_deref()
                .unwrap_or("")
                .cmp(right.fp.as_deref().unwrap_or(""))
        })
        .then_with(|| left.form.cmp(&right.form))
        .then_with(|| {
            left.frame
                .as_deref()
                .unwrap_or("")
                .cmp(right.frame.as_deref().unwrap_or(""))
        })
        .then_with(|| left.val.cmp(&right.val))
}

fn taxonomy_facts(
    cik: u64,
    taxonomy: &str,
    tag: &str,
    node: &Value,
) -> Result<Vec<XbrlFact>, ReferenceError> {
    let Some(node) = node.as_object() else {
        return Err(ReferenceError::edgar(format!(
            "companyfacts tag {taxonomy}:{tag} must be an object"
        )));
    };
    let Some(units) = node.get("units").and_then(Value::as_object) else {
        return Err(ReferenceError::edgar(format!(
            "companyfacts tag {taxonomy}:{tag} is missing its units object"
        )));
    };
    let mut facts: Vec<XbrlFact> = Vec::new();
    for (unit_name, entries) in units {
        facts.extend(unit_facts(cik, taxonomy, tag, unit_name, entries)?);
    }
    Ok(facts)
}

fn unit_facts(
    cik: u64,
    taxonomy: &str,
    tag: &str,
    unit: &str,
    entries: &Value,
) -> Result<Vec<XbrlFact>, ReferenceError> {
    let Some(entries) = entries.as_array() else {
        return Err(ReferenceError::edgar(format!(
            "companyfacts unit {taxonomy}:{tag} {unit:?} must be an array"
        )));
    };
    let mut facts: Vec<XbrlFact> = Vec::new();
    for (entry_number, entry) in entries.iter().enumerate() {
        let Some(entry) = entry.as_object() else {
            return Err(ReferenceError::edgar(format!(
                "companyfacts entry {taxonomy}:{tag} {unit:?}[{entry_number}] must be an object"
            )));
        };
        facts.push(entry_fact(cik, taxonomy, tag, unit, entry, entry_number)?);
    }
    Ok(facts)
}

fn entry_fact(
    cik: u64,
    taxonomy: &str,
    tag: &str,
    unit: &str,
    entry: &serde_json::Map<String, Value>,
    entry_number: usize,
) -> Result<XbrlFact, ReferenceError> {
    let context = format!("{taxonomy}:{tag} '{unit}'[{entry_number}]");
    Ok(XbrlFact {
        cik,
        taxonomy: taxonomy.to_string(),
        tag: tag.to_string(),
        unit: unit.to_string(),
        period_end: date_of(entry, "end", &context)?,
        fy: optional_integer(entry, "fy", &context)?,
        fp: optional_text(entry, "fp", &context)?,
        form: required_text(entry, "form", &context)?,
        accn: required_text(entry, "accn", &context)?,
        filed: date_of(entry, "filed", &context)?,
        frame: optional_text(entry, "frame", &context)?,
        val: decimal_of(entry, &context)?,
    })
}

/// Format a companyfacts request URL from the configured template.
pub fn company_facts_url(template: &str, cik: u64) -> Result<String, ReferenceError> {
    format_template(
        template,
        &[("cik", cik.to_string())],
        "invalid SEC facts URL template",
    )
}

/// Format a companyconcept request URL from the configured template.
pub fn company_concept_url(
    template: &str,
    cik: u64,
    taxonomy: &str,
    tag: &str,
) -> Result<String, ReferenceError> {
    format_template(
        template,
        &[
            ("cik", cik.to_string()),
            ("taxonomy", taxonomy.to_string()),
            ("tag", tag.to_string()),
        ],
        "invalid SEC facts URL template",
    )
}

/// Render a Python `str.format` URL template with the supplied values.
///
/// Supports the subset the committed configuration uses: literal text,
/// `{{`/`}}` escapes, named replacement fields, and a numeric format spec of
/// the form `[[fill]align][0][width]`. Unknown names, positional `{}` fields,
/// and unsupported specs fail exactly like Python's `format` fails the
/// template validators.
pub fn format_template(
    template: &str,
    values: &[(&str, String)],
    message: &str,
) -> Result<String, ReferenceError> {
    let invalid = || ReferenceError::invalid(format!("{message}: {template:?}"));
    let mut rendered = String::new();
    let mut characters = template.chars().peekable();
    while let Some(character) = characters.next() {
        match character {
            '{' => {
                if characters.peek() == Some(&'{') {
                    characters.next();
                    rendered.push('{');
                    continue;
                }
                let mut name = String::new();
                let mut spec: Option<String> = None;
                let mut terminated = false;
                let mut in_spec = false;
                for next in characters.by_ref() {
                    match next {
                        '}' => {
                            terminated = true;
                            break;
                        }
                        ':' if !in_spec => in_spec = true,
                        other if in_spec => {
                            spec.get_or_insert_with(String::new).push(other);
                        }
                        other => name.push(other),
                    }
                }
                if !terminated {
                    return Err(invalid());
                }
                let value = values
                    .iter()
                    .find(|(key, _)| *key == name.as_str())
                    .map(|(_, value)| value.clone())
                    .ok_or_else(invalid)?;
                match spec {
                    None => rendered.push_str(&value),
                    Some(spec) => {
                        rendered.push_str(&apply_format_spec(&value, &spec).ok_or_else(invalid)?)
                    }
                }
            }
            '}' => {
                if characters.peek() == Some(&'}') {
                    characters.next();
                    rendered.push('}');
                } else {
                    return Err(invalid());
                }
            }
            other => rendered.push(other),
        }
    }
    Ok(rendered)
}

/// Apply a Python numeric format spec: `[[fill]align][0][width]`.
fn apply_format_spec(value: &str, spec: &str) -> Option<String> {
    let mut fill = ' ';
    let mut align: Option<char> = None;
    let mut rest = spec;
    let mut characters = rest.chars();
    let first = characters.next();
    let second = characters.next();
    match (first, second) {
        (Some(fill_char), Some(align_char)) if "<>=^".contains(align_char) => {
            fill = fill_char;
            align = Some(align_char);
            rest = &spec[2.min(spec.chars().count())..];
        }
        (Some(align_char), _) if "<>=^".contains(align_char) => {
            align = Some(align_char);
            rest = &spec[1..];
        }
        (Some('0'), _) => {
            fill = '0';
            rest = &spec[1..];
        }
        _ => {}
    }
    if rest.is_empty() {
        return Some(align_value(
            value,
            align.unwrap_or(default_align(value)),
            fill,
            0,
        ));
    }
    if !rest.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let width: usize = rest.parse().ok()?;
    Some(align_value(
        value,
        align.unwrap_or(default_align(value)),
        fill,
        width,
    ))
}

fn default_align(value: &str) -> char {
    if value
        .bytes()
        .all(|byte| byte.is_ascii_digit() || byte == b'-')
    {
        '>'
    } else {
        '<'
    }
}

fn align_value(value: &str, align: char, fill: char, width: usize) -> String {
    let padding = width.saturating_sub(value.chars().count());
    match align {
        '<' => format!("{value}{}", fill.to_string().repeat(padding)),
        '>' => format!("{}{value}", fill.to_string().repeat(padding)),
        '^' => {
            let left = padding / 2;
            let right = padding - left;
            format!(
                "{}{value}{}",
                fill.to_string().repeat(left),
                fill.to_string().repeat(right)
            )
        }
        _ => format!("{value}{}", fill.to_string().repeat(padding)),
    }
}

fn document(payload: &[u8]) -> Result<Value, ReferenceError> {
    let value: Value = serde_json::from_slice(payload).map_err(|error| {
        ReferenceError::edgar(format!("SEC facts payload is not valid JSON: {error}"))
    })?;
    if !value.is_object() {
        return Err(ReferenceError::edgar(
            "SEC facts payload root must be an object",
        ));
    }
    Ok(value)
}

fn cik_of(document: &Value) -> Result<u64, ReferenceError> {
    let value = document
        .as_object()
        .and_then(|object| object.get("cik"))
        .ok_or_else(|| {
            ReferenceError::edgar("SEC facts payload is missing a positive integer cik")
        })?;
    if value.is_boolean() {
        return Err(ReferenceError::edgar(
            "SEC facts payload is missing a positive integer cik",
        ));
    }
    let Some(integer) = value.as_i64() else {
        return Err(ReferenceError::edgar(
            "SEC facts payload is missing a positive integer cik",
        ));
    };
    if integer <= 0 {
        return Err(ReferenceError::edgar(
            "SEC facts payload is missing a positive integer cik",
        ));
    }
    u64::try_from(integer)
        .map_err(|_| ReferenceError::edgar("SEC facts payload is missing a positive integer cik"))
}

fn mapping_of<'a>(
    document: &'a Value,
    key: &str,
    context: &str,
) -> Result<&'a serde_json::Map<String, Value>, ReferenceError> {
    document
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_object)
        .ok_or_else(|| {
            ReferenceError::edgar(format!("{context} payload is missing the {key:?} object"))
        })
}

fn text_of(document: &Value, key: &str, context: &str) -> Result<String, ReferenceError> {
    let value = document
        .as_object()
        .and_then(|object| object.get(key))
        .ok_or_else(|| ReferenceError::edgar(format!("{context} payload is missing {key:?}")))?;
    let Some(text) = value.as_str() else {
        return Err(ReferenceError::edgar(format!(
            "{context} payload is missing {key:?}"
        )));
    };
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err(ReferenceError::edgar(format!(
            "{context} payload is missing {key:?}"
        )));
    }
    Ok(trimmed.to_string())
}

fn required_text(
    entry: &serde_json::Map<String, Value>,
    key: &str,
    context: &str,
) -> Result<String, ReferenceError> {
    let value = entry
        .get(key)
        .ok_or_else(|| ReferenceError::edgar(format!("SEC entry {context} is missing {key:?}")))?;
    let Some(text) = value.as_str() else {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} is missing {key:?}"
        )));
    };
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} is missing {key:?}"
        )));
    }
    Ok(trimmed.to_string())
}

fn optional_text(
    entry: &serde_json::Map<String, Value>,
    key: &str,
    context: &str,
) -> Result<Option<String>, ReferenceError> {
    let Some(value) = entry.get(key) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let Some(text) = value.as_str() else {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} has an invalid {key:?}"
        )));
    };
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} has an invalid {key:?}"
        )));
    }
    Ok(Some(trimmed.to_string()))
}

fn optional_integer(
    entry: &serde_json::Map<String, Value>,
    key: &str,
    context: &str,
) -> Result<Option<i32>, ReferenceError> {
    let Some(value) = entry.get(key) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    if value.is_boolean() {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} has a non-integer {key:?}"
        )));
    }
    let Some(integer) = value.as_i64() else {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} has a non-integer {key:?}"
        )));
    };
    i32::try_from(integer).map(Some).map_err(|_| {
        ReferenceError::edgar(format!("SEC entry {context} has a non-integer {key:?}"))
    })
}

fn decimal_of(
    entry: &serde_json::Map<String, Value>,
    context: &str,
) -> Result<Decimal18, ReferenceError> {
    let value = entry.get("val").ok_or_else(|| {
        ReferenceError::edgar(format!("SEC entry {context} has a non-numeric 'val'"))
    })?;
    if value.is_boolean() {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} has a non-numeric 'val'"
        )));
    }
    let text = match value {
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                integer.to_string()
            } else if let Some(integer) = number.as_u64() {
                integer.to_string()
            } else if let Some(float) = number.as_f64() {
                format!("{float}")
            } else {
                return Err(ReferenceError::edgar(format!(
                    "SEC entry {context} has a non-numeric 'val'"
                )));
            }
        }
        Value::String(text) => text.clone(),
        _ => {
            return Err(ReferenceError::edgar(format!(
                "SEC entry {context} has a non-numeric 'val'"
            )))
        }
    };
    match Decimal18::parse(&text) {
        Ok(decimal) => Ok(decimal),
        Err(_) if is_non_finite_decimal(&text) => Err(ReferenceError::edgar(format!(
            "SEC entry {context} has a non-finite 'val'"
        ))),
        Err(_) => Err(ReferenceError::edgar(format!(
            "SEC entry {context} has a non-numeric 'val'"
        ))),
    }
}

fn date_of(
    entry: &serde_json::Map<String, Value>,
    key: &str,
    context: &str,
) -> Result<Date, ReferenceError> {
    let value = entry
        .get(key)
        .ok_or_else(|| ReferenceError::edgar(format!("SEC entry {context} is missing {key:?}")))?;
    let Some(text) = value.as_str() else {
        return Err(ReferenceError::edgar(format!(
            "SEC entry {context} is missing {key:?}"
        )));
    };
    Date::parse(text.trim())
        .ok_or_else(|| ReferenceError::edgar(format!("SEC entry {context} has an invalid {key:?}")))
}

#[cfg(test)]
#[path = "../tests/unit/edgar.rs"]
mod tests;
