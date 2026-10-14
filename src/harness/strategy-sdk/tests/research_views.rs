//! Public publication contract: strict decoding, causal evidence and open compositions.
use harness_strategy_sdk::{
    research::{
        recipes, validate_publication, Dataset, DatasetSemantics, Field, FieldType, Panel,
        ResearchView, WaveletHistory,
    },
    serde_json::{json, Value},
    StudyOutput,
};
use std::collections::BTreeMap;

fn field(name: &str, field_type: FieldType) -> Field {
    Field {
        name: name.into(),
        label: name.into(),
        description: format!("Published test {name} field"),
        field_type,
        unit: None,
    }
}
fn view() -> ResearchView {
    ResearchView {
        schema_version: 1,
        title: "Published observation".into(),
        description: String::new(),
        limitations: vec!["Demonstration evidence".into()],
        datasets: vec![Dataset {
            id: "data".into(),
            output: "observations".into(),
            fields: vec![
                field("id", FieldType::String),
                field("time", FieldType::TimestampNs),
                field("available", FieldType::TimestampNs),
                field("value", FieldType::Number),
            ],
            identity: Some("id".into()),
            selection_group: Some("observations".into()),
            event_time: Some("time".into()),
            available_at: Some("available".into()),
        }],
        panels: vec![Panel {
            id: "signal".into(),
            kind: "line".into(),
            title: "Observed signal".into(),
            dataset: "data".into(),
            encodings: BTreeMap::from([("x".into(), "time".into()), ("y".into(), "value".into())]),
            description: None,
        }],
    }
}
fn publication(view: ResearchView, rows: Vec<Vec<Value>>) -> BTreeMap<String, Value> {
    BTreeMap::from([
        (
            "observations".into(),
            json!(StudyOutput::Table {
                columns: vec![
                    "id".into(),
                    "time".into(),
                    "available".into(),
                    "value".into()
                ],
                rows
            }),
        ),
        ("view".into(), json!(StudyOutput::ResearchView { view })),
    ])
}
#[test]
fn complete_publication_rejects_broken_references_types_identity_and_future_values() {
    let rows = vec![vec![
        json!("a"),
        json!("1783958220000000000"),
        json!("1783958280000000000"),
        json!(0.5),
    ]];
    let valid = publication(view(), rows.clone());
    validate_publication(&valid).unwrap();
    let mut missing = valid.clone();
    missing.remove("observations");
    assert!(validate_publication(&missing).is_err());
    let mut duplicate = rows.clone();
    duplicate.extend(rows.clone());
    assert!(validate_publication(&publication(view(), duplicate)).is_err());
    let mut future = rows.clone();
    future[0][1] = json!("1783958290000000000");
    assert!(validate_publication(&publication(view(), future)).is_err());
    let mut wrong = rows;
    wrong[0][3] = json!("not numeric");
    assert!(validate_publication(&publication(view(), wrong)).is_err());
    let mut unknown = view();
    unknown.panels[0]
        .encodings
        .insert("y".into(), "missing".into());
    assert!(unknown.validate().is_err());
    let mut version = view();
    version.schema_version = 2;
    assert!(version.validate().is_err());
    let mut extension = view();
    extension.panels[0].kind = "custom/topology/v1".into();
    assert!(extension.validate().is_ok());
    let mut extra = json!(StudyOutput::ResearchView { view: view() });
    extra["view"]["script"] = json!("alert(1)");
    assert!(harness_strategy_sdk::serde_json::from_value::<StudyOutput>(extra).is_err());
}
#[test]
fn every_family_composes_without_a_study_specific_renderer() {
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
        let templates = recipes::recipe(family).unwrap();
        let mut datasets: BTreeMap<String, Dataset> = BTreeMap::new();
        let mut mappings = BTreeMap::new();
        for template in templates {
            let dataset = datasets
                .entry(template.slot.into())
                .or_insert_with(|| Dataset {
                    id: template.slot.into(),
                    output: template.slot.into(),
                    fields: vec![field("identity", FieldType::String)],
                    identity: Some("identity".into()),
                    selection_group: None,
                    event_time: None,
                    available_at: None,
                });
            for (_, role) in template.encodings {
                let name = format!("column_{role}");
                mappings.insert(format!("{}.{role}", template.slot), name.clone());
                if !dataset.fields.iter().any(|f| f.name == name) {
                    dataset.fields.push(field(&name, FieldType::Number));
                }
            }
        }
        let composed = recipes::compose(family, family, datasets, mappings).unwrap();
        let wire = json!(StudyOutput::ResearchView { view: composed });
        harness_strategy_sdk::serde_json::from_value::<StudyOutput>(wire)
            .unwrap()
            .validate()
            .unwrap();
    }
}
#[test]
fn wavelet_history_publishes_actual_coefficients_and_exact_causal_coordinates() {
    let mut history = WaveletHistory::default();
    history
        .observe("session-a", "AAA", 10, 20, &[1., 1., 3., 3.])
        .unwrap();
    let StudyOutput::Table { columns, rows } = history.output() else {
        panic!("expected coefficient evidence")
    };
    assert_eq!(rows.len(), 3);
    assert_eq!(columns[3], "event_time_ns");
    assert_eq!(rows[0][3], json!("10"));
    assert!((rows[2][7].as_f64().unwrap() + 2.0).abs() < 1e-12);
    assert!(history
        .observe("session-a", "AAA", 21, 20, &[1., 1., 3., 3.])
        .is_err());
}

#[test]
fn standalone_semantics_validate_complete_grain_and_reject_conflicting_bindings() {
    let saved = view();
    let semantics = DatasetSemantics {
        schema_version: 1,
        dataset: saved.datasets[0].clone(),
        row_meaning: "One observed signal at one time".into(),
        grain: vec!["time".into()],
        dimensions: vec!["time".into()],
        measures: vec!["value".into()],
        ordered: vec!["time".into()],
        preferred_views: saved.panels.clone(),
    };
    let mut outputs = publication(
        saved.clone(),
        vec![vec![json!("a"), json!(10), json!(20), json!(0.5)]],
    );
    outputs.remove("view");
    outputs.insert(
        "semantics".into(),
        json!(StudyOutput::DatasetSemantics {
            semantics: semantics.clone()
        }),
    );
    validate_publication(&outputs).unwrap();
    // Distinct identities still fail when the semantic row key duplicates.
    outputs.get_mut("observations").unwrap()["rows"]
        .as_array_mut()
        .unwrap()
        .push(json!(["b", 10, 20, 0.7]));
    assert!(validate_publication(&outputs)
        .unwrap_err()
        .contains("grain"));
    outputs.get_mut("observations").unwrap()["rows"] = json!([["a", 10, 20, 0.5]]);
    let mut conflicting = saved;
    conflicting.datasets[0].fields[3].unit = Some("bp".into());
    outputs.insert(
        "view".into(),
        json!(StudyOutput::ResearchView { view: conflicting }),
    );
    assert!(validate_publication(&outputs)
        .unwrap_err()
        .contains("conflicts"));
    let mut bad = semantics.clone();
    bad.measures.push("id".into());
    assert!(bad.validate().is_err());
    let mut wire = json!(StudyOutput::DatasetSemantics { semantics });
    wire["semantics"]["script"] = json!("execute()");
    assert!(harness_strategy_sdk::serde_json::from_value::<StudyOutput>(wire).is_err());
}
