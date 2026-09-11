use super::*;
use crate::sweeps::spec::SweepPoint;

fn point(run_id: &str, level: &str) -> SweepPoint {
    SweepPoint {
        index: 0,
        parameters: BTreeMap::from([(
            "fast".to_string(),
            JsonValue::object([("$decimal", JsonValue::String(level.to_string()))]),
        )]),
        configuration_digest: format!("digest-{run_id}"),
        run_id: run_id.to_string(),
    }
}

fn record(run_id: &str, value: &str) -> RunRecord {
    RunRecord {
        run_id: run_id.to_string(),
        configuration_digest: Some(format!("digest-{run_id}")),
        metrics: BTreeMap::from([("sharpe".to_string(), value.to_string())]),
    }
}

#[test]
fn ranks_by_value_then_run_id() {
    let records = vec![record("b", "1.5"), record("a", "1.5"), record("c", "0.5")];
    let ranked = rank_runs(&records, "sharpe", true).expect("rank");
    assert_eq!(
        ranked
            .iter()
            .map(|run| run.run_id.as_str())
            .collect::<Vec<_>>(),
        vec!["a", "b", "c"]
    );
    assert_eq!(ranked[0].rank, 1);
}

#[test]
fn skips_unparseable_metrics() {
    let records = vec![record("a", "not-a-number"), record("b", "2")];
    let ranked = rank_runs(&records, "sharpe", true).expect("rank");
    assert_eq!(ranked.len(), 1);
    assert_eq!(ranked[0].run_id, "b");
}

#[test]
fn sensitivity_summarizes_parameter_levels() {
    let points = vec![point("r1", "5"), point("r2", "10"), point("r3", "5")];
    let records = vec![record("r1", "1"), record("r2", "3"), record("r3", "2")];
    let summary = parameter_sensitivity(&points, &records, "sharpe").expect("sensitivity");
    assert_eq!(summary.runs_used, 3);
    let parameter = &summary.parameters[0];
    assert_eq!(parameter.name, "fast");
    // Level 5 has mean 1.5 over two runs; level 10 has mean 3.
    assert_eq!(parameter.best_value, "{\"$decimal\":\"10\"}");
    assert_eq!(parameter.effect_range.to_string(), "1.500000000000000000");
}
