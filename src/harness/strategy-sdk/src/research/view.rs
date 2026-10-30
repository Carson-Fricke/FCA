//! Versioned presentation contracts. They carry no execution capabilities.
use std::collections::BTreeMap;
use std::collections::BTreeSet;

use serde::Deserialize;
use serde::Serialize;

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
            return Err(format!("research_view: schema_version={}, title={:?}; require schema_version=1 and a nonempty title", self.schema_version, self.title));
        }
        if self.datasets.is_empty()
            || self.datasets.len() > 32
            || self.panels.is_empty()
            || self.panels.len() > 24
        {
            return Err(format!("research_view: received {} datasets and {} panels; require 1..=32 datasets and 1..=24 panels", self.datasets.len(), self.panels.len()));
        }
        let mut datasets = BTreeMap::new();
        for dataset in &self.datasets {
            if dataset.id.trim().is_empty()
                || dataset.output.trim().is_empty()
                || datasets.insert(&dataset.id, dataset).is_some()
            {
                return Err(
                    format!("dataset id={:?}, output={:?}: identities and output references must be nonempty and IDs unique", dataset.id, dataset.output),
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
                return Err(format!("dataset {:?}, output {:?}: fields must have unique nonempty names and nonempty labels/descriptions; received {:?}", dataset.id, dataset.output, dataset.fields));
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
                    return Err(format!("dataset {:?}: identity/timing references unknown field {name:?}; declared fields {:?}", dataset.id, fields));
                }
            }
            if dataset.selection_group.is_some() && dataset.identity.is_none() {
                return Err(format!(
                    "dataset {:?}: selection_group requires a declared identity field",
                    dataset.id
                ));
            }
        }
        let mut ids = BTreeSet::new();
        for panel in &self.panels {
            if panel.id.trim().is_empty()
                || panel.title.trim().is_empty()
                || panel.kind.trim().is_empty()
                || !ids.insert(&panel.id)
            {
                return Err(format!("panel id={:?}, kind={:?}, title={:?}: require a unique nonempty ID and nonempty kind/title", panel.id, panel.kind, panel.title));
            }
            let dataset = datasets.get(&panel.dataset).ok_or_else(|| {
                format!(
                    "panel {:?} references unknown dataset {:?}; available IDs {:?}",
                    panel.id,
                    panel.dataset,
                    datasets.keys().collect::<Vec<_>>()
                )
            })?;
            for field in panel.encodings.values() {
                if !dataset.fields.iter().any(|f| &f.name == field) {
                    return Err(format!(
                        "panel {:?}, dataset {:?}: unknown encoding field {field:?}",
                        panel.id, panel.dataset
                    ));
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
            return Err(format!("dataset {:?}: require schema_version=1, nonempty row_meaning and grain; received schema_version={}, row_meaning={:?}, grain={:?}", self.dataset.id, self.schema_version, self.row_meaning, self.grain));
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
            return Err(format!("dataset id={:?}, output={:?}: require nonempty identity/output and unique fields with nonempty names, labels and descriptions; received {:?}", self.dataset.id, self.dataset.output, self.dataset.fields));
        }
        for (role, names) in [
            ("grain", &self.grain),
            ("dimensions", &self.dimensions),
            ("measures", &self.measures),
            ("ordered", &self.ordered),
        ] {
            if names.iter().collect::<BTreeSet<_>>().len() != names.len()
                || names.iter().any(|n| !fields.contains_key(n.as_str()))
            {
                return Err(format!("dataset {:?}, {role}={names:?}: require unique references to declared fields {:?}", self.dataset.id, fields.keys().collect::<Vec<_>>()));
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
                format!("dataset {:?}: measures {:?} must be numeric and disjoint from identity/dimensions {:?}; ordered {:?} must reference numeric/timestamp dimensions", self.dataset.id, self.measures, self.dimensions, self.ordered),
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
                return Err(format!(
                    "dataset {:?}: unknown identity/timing field {name:?}",
                    self.dataset.id
                ));
            }
        }
        if self.dataset.selection_group.is_some() && self.dataset.identity.is_none() {
            return Err(format!(
                "dataset {:?}: selection_group requires a declared identity field",
                self.dataset.id
            ));
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
                    format!("dataset {:?}, preferred panel {:?}: require nonempty unique panel ID, kind/title and field encodings bound to dataset {:?}; received {panel:?}", self.dataset.id, panel.id, self.dataset.id),
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
        StudyOutput::CausalObservations { dataset } => {
            (dataset.columns.clone(), dataset.rows.clone())
        }
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
            .ok_or_else(|| format!("dataset {:?}, output {:?}: missing declared field {:?}; available columns {:?}", dataset.id, dataset.output, field.name, columns))?;
        for (row_index, row) in rows.iter().enumerate() {
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
                return Err(format!("dataset {:?}, output {:?}, rows[{row_index}], field {:?}: received {cell}; expected {:?}", dataset.id, dataset.output, field.name, field.field_type));
            }
        }
    }
    if let Some(identity) = &dataset.identity {
        let index = columns
            .iter()
            .position(|c| c == identity)
            .ok_or("missing identity")?;
        let mut seen = BTreeSet::new();
        for (row_index, row) in rows.iter().enumerate() {
            if row[index].is_null() || !seen.insert(row[index].to_string()) {
                return Err(format!("dataset {:?}, rows[{row_index}].{identity}: identity {} must be nonnull and unique", dataset.id, row[index]));
            }
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
        for (row_index, row) in rows.iter().enumerate() {
            if !matches!((integer(&row[ei]), integer(&row[ai])), (Some(e), Some(a)) if e <= a) {
                return Err(format!("dataset {:?}, rows[{row_index}]: require exact nanosecond {event} <= {available}; received {} and {}", dataset.id, row[ei], row[ai]));
            }
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
        for (row_index, row) in rows.iter().enumerate() {
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
                    format!("dataset {:?}, rows[{row_index}]: grain {:?} has null or duplicate key {:?}; keys must be nonnull and unique across the complete output", dataset.id, grain, values),
                );
            }
        }
    }
    Ok(())
}
