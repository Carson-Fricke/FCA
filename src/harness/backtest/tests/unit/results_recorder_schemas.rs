use super::*;

#[test]
fn artifact_names_round_trip() {
    for artifact in Artifact::ALL {
        assert_eq!(Artifact::parse(artifact.value()), Some(artifact));
    }
    assert_eq!(Artifact::parse("nope"), None);
}

#[test]
fn stream_artifacts_exclude_the_manifest() {
    assert_eq!(STREAM_ARTIFACTS.len(), Artifact::ALL.len() - 1);
    assert!(!STREAM_ARTIFACTS.contains(&Artifact::Manifest));
}

#[test]
fn every_field_name_is_unique_within_a_schema() {
    for artifact in Artifact::ALL {
        let fields = artifact.fields();
        let mut names: Vec<&str> = fields.iter().map(|field| field.name).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), fields.len(), "{artifact:?}");
    }
}

#[test]
fn schema_metadata_matches_python() {
    let metadata = Artifact::Actions.metadata();
    assert_eq!(
        metadata[0],
        ("harness.schema", "recorder.actions".to_string())
    );
    assert_eq!(metadata[1], ("harness.schema.version", "1".to_string()));
    assert_eq!(
        metadata[2],
        ("harness.decimal.encoding", "string-exact".to_string())
    );
    assert_eq!(Artifact::ExecutionReports.schema_name(), "execution_report");
}

#[test]
fn actions_field_order_matches_python() {
    let names: Vec<&str> = Artifact::Actions
        .fields()
        .iter()
        .map(|field| field.name)
        .collect();
    assert_eq!(
        names,
        vec![
            "action_id",
            "sequence",
            "timestamp",
            "strategy_id",
            "book_id",
            "kind",
            "symbol",
            "side",
            "quantity",
            "order_id",
            "reason",
            "detail",
        ]
    );
}
