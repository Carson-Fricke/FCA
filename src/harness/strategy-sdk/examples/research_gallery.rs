//! Generate UI demonstration evidence through the real SDK contracts.
//! These deterministic signals are not market observations or profitable strategies.
use harness_strategy_sdk::{
    research::{
        recipes, validate_publication, Dataset, DatasetSemantics, Field, FieldType, WaveletHistory,
    },
    serde_json::{json, Value},
    StudyOutput,
};
use std::collections::{BTreeMap, BTreeSet};

fn main() {
    let mut programs = BTreeMap::new();
    for family in [
        "wavelets",
        "seasonality",
        "events",
        "regimes",
        "predictive_signals",
        "liquidity",
        "parameters",
        "falsification",
        "learned_models",
    ] {
        let mut descriptors: BTreeMap<String, Dataset> = BTreeMap::new();
        let mut mappings = BTreeMap::new();
        let mut outputs = BTreeMap::new();
        for template in recipes::recipe(family).unwrap() {
            let dataset = descriptors
                .entry(template.slot.into())
                .or_insert_with(|| Dataset {
                    id: template.slot.into(),
                    output: template.slot.into(),
                    fields: vec![Field {
                        name: "id".into(),
                        label: "Observation".into(),
                        description: "Unique identity of this synthetic observation.".into(),
                        field_type: FieldType::String,
                        unit: None,
                    }],
                    identity: Some("id".into()),
                    selection_group: None,
                    event_time: None,
                    available_at: None,
                });
            for (_, role) in template.encodings {
                let name = (*role).to_string();
                mappings.insert(format!("{}.{role}", template.slot), name.clone());
                if !dataset.fields.iter().any(|f| f.name == name) {
                    let categorical = [
                        "session", "event", "state", "from", "to", "bucket", "control", "sample",
                        "scope", "feature", "claim", "test",
                    ]
                    .contains(role);
                    dataset.fields.push(Field {
                        name: name.clone(),
                        label: name.replace('_', " "),
                        description: format!(
                            "Synthetic {family} {name} coordinate or measurement in {} evidence.",
                            template.slot
                        ),
                        field_type: if categorical {
                            FieldType::String
                        } else {
                            FieldType::Number
                        },
                        unit: None,
                    });
                }
            }
        }
        if family == "parameters" {
            descriptors
                .get_mut("experiments")
                .unwrap()
                .fields
                .push(Field {
                    name: "horizon".into(),
                    label: "horizon".into(),
                    description: "Number of sessions in the synthetic outcome horizon.".into(),
                    field_type: FieldType::Number,
                    unit: Some("sessions".into()),
                });
        }
        for (slot, dataset) in &descriptors {
            let columns = dataset
                .fields
                .iter()
                .map(|f| f.name.clone())
                .collect::<Vec<_>>();
            let count =
                if ["calibration", "controls", "subsamples", "features"].contains(&slot.as_str()) {
                    4
                } else if slot == "transitions" {
                    16
                } else {
                    24
                };
            let rows = (0..count)
                .map(|i| {
                    dataset
                        .fields
                        .iter()
                        .map(|f| {
                            if f.name == "id" {
                                json!(format!("{slot}-{i}"))
                            } else if f.field_type == FieldType::String {
                                json!(format!(
                                    "{} {}",
                                    f.name,
                                    if f.name == "session" || f.name == "event" {
                                        i / 8
                                    } else if f.name == "from" {
                                        i / 4
                                    } else {
                                        i % 4
                                    }
                                ))
                            } else {
                                json!(match f.name.as_str() {
                                    "minute" | "relative_time" | "parameter_x" => (i % 8) as f64,
                                    "horizon" if family == "parameters" => (i % 2) as f64,
                                    "time" | "start" | "horizon" => i as f64,
                                    "duration" =>
                                        if slot == "events" {
                                            (i % 8) as f64
                                        } else {
                                            i as f64
                                        },
                                    "end" => i as f64 + 0.7,
                                    "parameter_y" | "scale" => (i / 8) as f64,
                                    "fine" => 0.2 + (i % 5) as f64 * 0.07,
                                    "mid" => 0.2,
                                    "coarse" => 0.6 - (i % 5) as f64 * 0.07,
                                    "count" => ((i % 5) + 1) as f64,
                                    "probability" => 0.25,
                                    _ => (i as f64 * 0.4).sin(),
                                })
                            }
                        })
                        .collect::<Vec<_>>()
                })
                .collect::<Vec<_>>();
            outputs.insert(slot.clone(), json!(StudyOutput::Table { columns, rows }));
        }
        if family == "wavelets" {
            let values = (0..128)
                .map(|i| (i as f64 * 0.18).sin() + (i as f64 * 0.71).cos() * 0.25)
                .collect::<Vec<_>>();
            let mut history = WaveletHistory::default();
            for time in (32..128).step_by(3) {
                history
                    .observe(
                        "synthetic-session",
                        "DEMO",
                        time as i64,
                        time as i64,
                        &values[time - 32..time],
                    )
                    .unwrap();
            }
            outputs.insert("energy".into(), json!(history.energy_output()));
            outputs.insert("coefficients".into(), json!(history.output()));
            let dataset = descriptors.get_mut("energy").unwrap();
            dataset.fields = vec![
                Field {
                    name: "observation_id".into(),
                    label: "Observation".into(),
                    description: "Unique identity of this synthetic observation.".into(),
                    field_type: FieldType::String,
                    unit: None,
                },
                Field {
                    name: "event_time_ns".into(),
                    label: "Demonstration time".into(),
                    description:
                        "Time at which the synthetic observation occurred, in epoch nanoseconds."
                            .into(),
                    field_type: FieldType::TimestampNs,
                    unit: Some("ns".into()),
                },
                Field {
                    name: "available_at_ns".into(),
                    label: "Availability".into(),
                    description:
                        "Earliest time the observation was available, in epoch nanoseconds.".into(),
                    field_type: FieldType::TimestampNs,
                    unit: Some("ns".into()),
                },
                Field {
                    name: "support".into(),
                    label: "Haar support".into(),
                    description: "Number of trailing observations covered by this Haar scale."
                        .into(),
                    field_type: FieldType::Number,
                    unit: Some("observations".into()),
                },
                Field {
                    name: "energy".into(),
                    label: "Mean squared detail coefficient".into(),
                    description:
                        "Mean squared Haar detail coefficient at this observation and scale.".into(),
                    field_type: FieldType::Number,
                    unit: None,
                },
            ];
            dataset.identity = Some("observation_id".into());
            dataset.event_time = Some("event_time_ns".into());
            dataset.available_at = Some("available_at_ns".into());
            mappings.insert("energy.time".into(), "event_time_ns".into());
            mappings.insert("energy.scale".into(), "support".into());
            let dataset = descriptors.get_mut("price").unwrap();
            dataset.fields = vec![
                Field {
                    name: "id".into(),
                    label: "Observation".into(),
                    description: "Unique identity of this synthetic observation.".into(),
                    field_type: FieldType::String,
                    unit: None,
                },
                Field {
                    name: "time".into(),
                    label: "Demonstration time".into(),
                    description: "Time at which the synthetic observation occurred, in epoch nanoseconds.".into(),
                    field_type: FieldType::TimestampNs,
                    unit: Some("ns".into()),
                },
                Field {
                    name: "price".into(),
                    label: "Synthetic signal".into(),
                    description: "Synthetic signal amplitude supplied to the demonstration wavelet transform.".into(),
                    field_type: FieldType::Number,
                    unit: None,
                },
            ];
            dataset.event_time = Some("time".into());
            outputs.insert(
                "price".into(),
                json!(StudyOutput::Table {
                    columns: vec!["id".into(), "time".into(), "price".into()],
                    rows: values
                        .iter()
                        .enumerate()
                        .map(|(i, v)| vec![
                            json!(format!("signal-{i}")),
                            json!(i.to_string()),
                            json!(v)
                        ])
                        .collect()
                }),
            );
        }
        let mut view =
            recipes::compose(family, &family.replace('_', " "), descriptors, mappings).unwrap();
        view.description =
            "SDK-generated composition demonstration — synthetic evidence, not a market study."
                .into();
        view.limitations=vec!["Demonstration datasets illustrate the contract and interactions. They establish no financial or statistical finding.".into()];
        // The example author declares roles from the recipe it chose; the UI never
        // guesses roles from numeric-looking column values.
        for dataset in &view.datasets {
            let panels = view
                .panels
                .iter()
                .filter(|p| p.dataset == dataset.id)
                .cloned()
                .collect::<Vec<_>>();
            let mut dimensions = BTreeSet::new();
            let mut measures = BTreeSet::new();
            let mut ordered = BTreeSet::new();
            for panel in &panels {
                let e = &panel.encodings;
                match panel.kind.as_str() {
                    "heatmap" => {
                        dimensions.insert(e["x"].clone());
                        dimensions.insert(e["y"].clone());
                        measures.insert(e["value"].clone());
                    }
                    "scatter" | "composition" => {
                        for channel in ["x", "y", "z", "color"] {
                            if let Some(field) = e.get(channel) {
                                measures.insert(field.clone());
                            }
                        }
                    }
                    "line" | "events" | "comparison" | "distribution" => {
                        dimensions.insert(e["x"].clone());
                        measures.insert(e["y"].clone());
                        if let Some(series) = e.get("series") {
                            dimensions.insert(series.clone());
                        }
                    }
                    "intervals" => {
                        dimensions.insert(e["x"].clone());
                        dimensions.insert(e["y"].clone());
                        dimensions.insert(e["end"].clone());
                    }
                    "graph" => {
                        dimensions.insert(e["source"].clone());
                        dimensions.insert(e["target"].clone());
                    }
                    _ => {}
                }
            }
            if family == "parameters" {
                dimensions.insert("horizon".into());
            }
            for field in &dataset.fields {
                if dimensions.contains(&field.name)
                    && matches!(field.field_type, FieldType::Number | FieldType::TimestampNs)
                {
                    ordered.insert(field.name.clone());
                }
            }
            let semantics = DatasetSemantics {
                schema_version: 1,
                dataset: dataset.clone(),
                row_meaning: format!("One synthetic {family} {} observation", dataset.output),
                grain: vec![dataset.identity.clone().unwrap()],
                dimensions: dimensions.into_iter().collect(),
                measures: measures.into_iter().collect(),
                ordered: ordered.into_iter().collect(),
                preferred_views: panels,
            };
            outputs.insert(
                format!("{}_semantics", dataset.output),
                json!(StudyOutput::DatasetSemantics { semantics }),
            );
        }
        outputs.insert(
            "research_view".into(),
            json!(StudyOutput::ResearchView { view }),
        );
        validate_publication(&outputs).unwrap();
        programs.insert(family, outputs);
    }
    let document: Value =
        json!({"schema_version":1,"job_id":"sdk-research-demonstration","programs":programs});
    println!(
        "{}",
        harness_strategy_sdk::serde_json::to_string_pretty(&document).unwrap()
    );
}
