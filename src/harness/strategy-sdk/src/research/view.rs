//! Versioned presentation contracts. They carry no execution capabilities.
use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::StudyOutput;

/// Scalar field meaning, independent of its rendered appearance.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FieldType {
    Number,
    String,
    Boolean,
    TimestampNs,
}

/// Explicit column semantics; null is always retained, never filled with zero.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Field {
    pub name: String,
    pub label: String,
    /// Scientific meaning, including what a dimension varies or a measure records.
    pub description: String,
    pub field_type: FieldType,
    pub unit: Option<String>,
}

/// A binding to one output in the same program instance and originating job.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dataset {
    pub id: String,
    pub output: String,
    pub fields: Vec<Field>,
    /// Identity must be unique within this dataset. No positional joins exist.
    pub identity: Option<String>,
    /// Equal selection groups link equal explicit identities between panels.
    pub selection_group: Option<String>,
    pub event_time: Option<String>,
    pub available_at: Option<String>,
}

/// One registered visual primitive. Extensions use their own versioned kind.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Panel {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub dataset: String,
    /// Field mappings such as x/y/value/color/category/label/id.
    pub encodings: BTreeMap<String, String>,
    pub description: Option<String>,
}

/// A saved exhibit; datasets remain the sole evidence authority.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchView {
    pub schema_version: u32,
    pub title: String,
    pub description: String,
    pub limitations: Vec<String>,
    pub datasets: Vec<Dataset>,
    pub panels: Vec<Panel>,
}

impl ResearchView {
    /// Validate descriptors and references without executing any presentation code.
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1 || self.title.trim().is_empty() {
            return Err("research view requires schema_version 1 and a title".into());
        }
        if self.datasets.is_empty()
            || self.datasets.len() > 32
            || self.panels.is_empty()
            || self.panels.len() > 24
        {
            return Err("research view requires 1..32 datasets and 1..24 panels".into());
        }
        let mut datasets = BTreeMap::new();
        for dataset in &self.datasets {
            if dataset.id.trim().is_empty()
                || dataset.output.trim().is_empty()
                || datasets.insert(&dataset.id, dataset).is_some()
            {
                return Err(
                    "dataset identities and output references must be nonempty and unique".into(),
                );
            }
            let fields: BTreeSet<_> = dataset.fields.iter().map(|field| &field.name).collect();
            if fields.is_empty()
                || fields.len() != dataset.fields.len()
                || dataset.fields.iter().any(|f| {
                    f.name.trim().is_empty()
                        || f.label.trim().is_empty()
                        || f.description.trim().is_empty()
                })
            {
                return Err("dataset fields require unique names and labels".into());
            }
            for name in [
                &dataset.identity,
                &dataset.event_time,
                &dataset.available_at,
            ]
            .into_iter()
            .flatten()
            {
                if !fields.contains(name) {
                    return Err(format!("unknown dataset field {name}"));
                }
            }
            if dataset.selection_group.is_some() && dataset.identity.is_none() {
                return Err("linked selections require explicit identity".into());
            }
        }
        let mut ids = BTreeSet::new();
        for panel in &self.panels {
            if panel.id.trim().is_empty()
                || panel.title.trim().is_empty()
                || panel.kind.trim().is_empty()
                || !ids.insert(&panel.id)
            {
                return Err("panels require unique identities, a kind and a title".into());
            }
            let dataset = datasets
                .get(&panel.dataset)
                .ok_or("panel references an unknown dataset")?;
            for field in panel.encodings.values() {
                if !dataset.fields.iter().any(|f| &f.name == field) {
                    return Err(format!("unknown encoding field {field}"));
                }
            }
        }
        Ok(())
    }

    /// Validate against the complete publication, after all output callbacks finish.
    pub fn validate_outputs(&self, outputs: &BTreeMap<String, StudyOutput>) -> Result<(), String> {
        self.validate()?;
        for dataset in &self.datasets {
            validate_dataset_outputs(dataset, outputs, &[])?;
        }
        Ok(())
    }
}

/// One canonical output description, also usable without a research exhibit.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatasetSemantics {
    pub schema_version: u32,
    pub dataset: Dataset,
    /// What one returned row represents; never an inferred statistical claim.
    pub row_meaning: String,
    /// Complete, nonnull row key validated over the entire publication.
    pub grain: Vec<String>,
    pub dimensions: Vec<String>,
    pub measures: Vec<String>,
    /// Numeric or exact timestamp dimensions with meaningful ordering.
    pub ordered: Vec<String>,
    pub preferred_views: Vec<Panel>,
}

impl DatasetSemantics {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1 || self.row_meaning.trim().is_empty() || self.grain.is_empty() {
            return Err("dataset semantics require schema 1, row meaning and grain".into());
        }
        let fields: BTreeMap<_, _> = self
            .dataset
            .fields
            .iter()
            .map(|f| (f.name.as_str(), f))
            .collect();
        if self.dataset.id.trim().is_empty()
            || self.dataset.output.trim().is_empty()
            || fields.is_empty()
            || fields.len() != self.dataset.fields.len()
            || self.dataset.fields.iter().any(|f| {
                f.name.trim().is_empty()
                    || f.label.trim().is_empty()
                    || f.description.trim().is_empty()
            })
        {
            return Err("dataset semantics require unique named fields and output identity".into());
        }
        for names in [&self.grain, &self.dimensions, &self.measures, &self.ordered] {
            if names.iter().collect::<BTreeSet<_>>().len() != names.len()
                || names.iter().any(|n| !fields.contains_key(n.as_str()))
            {
                return Err("semantic roles require unique declared fields".into());
            }
        }
        if self.measures.iter().any(|n| {
            fields[n.as_str()].field_type != FieldType::Number
                || self.dimensions.contains(n)
                || self.dataset.identity.as_ref() == Some(n)
        }) || self
            .dimensions
            .iter()
            .any(|n| self.dataset.identity.as_ref() == Some(n))
            || self.ordered.iter().any(|n| {
                !self.dimensions.contains(n)
                    || !matches!(
                        fields[n.as_str()].field_type,
                        FieldType::Number | FieldType::TimestampNs
                    )
            })
        {
            return Err(
                "semantic measures, dimensions and ordered coordinates have incompatible roles"
                    .into(),
            );
        }
        for name in [
            &self.dataset.identity,
            &self.dataset.event_time,
            &self.dataset.available_at,
        ]
        .into_iter()
        .flatten()
        {
            if !fields.contains_key(name.as_str()) {
                return Err("unknown dataset identity or timing field".into());
            }
        }
        if self.dataset.selection_group.is_some() && self.dataset.identity.is_none() {
            return Err("linked selections require explicit identity".into());
        }
        let mut ids = BTreeSet::new();
        for panel in &self.preferred_views {
            if panel.id.trim().is_empty()
                || panel.title.trim().is_empty()
                || panel.kind.trim().is_empty()
                || !ids.insert(&panel.id)
                || panel.dataset != self.dataset.id
                || panel
                    .encodings
                    .values()
                    .any(|name| !fields.contains_key(name.as_str()))
            {
                return Err(
                    "preferred views require unique identities and same-dataset field bindings"
                        .into(),
                );
            }
        }
        Ok(())
    }
    pub fn validate_outputs(&self, outputs: &BTreeMap<String, StudyOutput>) -> Result<(), String> {
        self.validate()?;
        validate_dataset_outputs(&self.dataset, outputs, &self.grain)
    }
}

fn validate_dataset_outputs(
    dataset: &Dataset,
    outputs: &BTreeMap<String, StudyOutput>,
    grain: &[String],
) -> Result<(), String> {
    let output = outputs
        .get(&dataset.output)
        .ok_or_else(|| format!("missing research output {}", dataset.output))?;
    output.validate()?;
    let (columns, rows) = match output {
        StudyOutput::Table { columns, rows } => (columns.clone(), rows.clone()),
        StudyOutput::Series { points } => {
            let columns = [
                "symbol",
                "interval",
                "event_time_ns",
                "available_at_ns",
                "value",
                "undefined_reason",
            ]
            .map(str::to_string)
            .to_vec();
            let rows = points
                .iter()
                .map(|p| {
                    vec![
                        p.symbol.clone().into(),
                        p.interval.clone().into(),
                        p.event_time_ns.into(),
                        p.available_at_ns.into(),
                        p.value.map_or(serde_json::Value::Null, Into::into),
                        p.undefined_reason
                            .clone()
                            .map_or(serde_json::Value::Null, Into::into),
                    ]
                })
                .collect();
            (columns, rows)
        }
        StudyOutput::Events { events } => {
            let mut columns: BTreeSet<String> = ["symbol", "event_time_ns", "available_at_ns"]
                .map(str::to_string)
                .into_iter()
                .collect();
            for e in events {
                columns.extend(e.fields.keys().cloned());
            }
            let columns: Vec<_> = columns.into_iter().collect();
            let rows = events
                .iter()
                .map(|e| {
                    columns
                        .iter()
                        .map(|c| match c.as_str() {
                            "symbol" => e.symbol.clone().into(),
                            "event_time_ns" => e.event_time_ns.into(),
                            "available_at_ns" => e.available_at_ns.into(),
                            _ => e.fields.get(c).cloned().unwrap_or_default(),
                        })
                        .collect()
                })
                .collect();
            (columns, rows)
        }
        _ => return Err("research datasets must bind tables, series or events".into()),
    };
    for field in &dataset.fields {
        let index = columns
            .iter()
            .position(|c| c == &field.name)
            .ok_or_else(|| format!("output lacks field {}", field.name))?;
        for row in &rows {
            let cell = &row[index];
            if cell.is_null() {
                continue;
            }
            let valid = match field.field_type {
                FieldType::String => cell.is_string(),
                FieldType::Boolean => cell.is_boolean(),
                FieldType::Number => {
                    cell.as_f64().is_some_and(f64::is_finite)
                        || cell
                            .as_str()
                            .and_then(|s| s.parse::<f64>().ok())
                            .is_some_and(f64::is_finite)
                }
                FieldType::TimestampNs => {
                    cell.as_i64().is_some()
                        || cell.as_str().and_then(|s| s.parse::<i64>().ok()).is_some()
                }
            };
            if !valid {
                return Err(format!("invalid value for field {}", field.name));
            }
        }
    }
    if let Some(identity) = &dataset.identity {
        let index = columns
            .iter()
            .position(|c| c == identity)
            .ok_or("missing identity")?;
        let mut seen = BTreeSet::new();
        if rows
            .iter()
            .any(|row| row[index].is_null() || !seen.insert(row[index].to_string()))
        {
            return Err("dataset identity must be nonnull and unique".into());
        }
    }
    if let (Some(event), Some(available)) = (&dataset.event_time, &dataset.available_at) {
        let ei = columns
            .iter()
            .position(|c| c == event)
            .ok_or("missing event time")?;
        let ai = columns
            .iter()
            .position(|c| c == available)
            .ok_or("missing availability time")?;
        let integer =
            |v: &serde_json::Value| v.as_i64().or_else(|| v.as_str()?.parse::<i64>().ok());
        if rows
            .iter()
            .any(|r| !matches!((integer(&r[ei]), integer(&r[ai])), (Some(e), Some(a)) if e <= a))
        {
            return Err("dataset requires causal event and availability timestamps".into());
        }
    }
    if !grain.is_empty() {
        let indexes = grain
            .iter()
            .map(|name| {
                columns
                    .iter()
                    .position(|c| c == name)
                    .ok_or("missing grain field")
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut keys = BTreeSet::new();
        for row in &rows {
            let values = indexes
                .iter()
                .map(|i| {
                    let cell = &row[*i];
                    if cell.is_null() {
                        return serde_json::Value::Null;
                    }
                    match dataset
                        .fields
                        .iter()
                        .find(|f| f.name == columns[*i])
                        .map(|f| &f.field_type)
                    {
                        Some(FieldType::TimestampNs) => serde_json::json!(cell
                            .as_i64()
                            .or_else(|| cell.as_str()?.parse::<i64>().ok())),
                        Some(FieldType::Number) => serde_json::json!(cell
                            .as_f64()
                            .or_else(|| cell.as_str()?.parse::<f64>().ok())),
                        _ => cell.clone(),
                    }
                })
                .collect::<Vec<_>>();
            if values.iter().any(serde_json::Value::is_null)
                || !keys.insert(serde_json::to_string(&values).map_err(|e| e.to_string())?)
            {
                return Err(
                    "dataset grain must be nonnull and unique across the complete output".into(),
                );
            }
        }
    }
    Ok(())
}
