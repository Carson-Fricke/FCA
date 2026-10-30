//! Public numeric and evidence rejection contracts, without an execution host.
use harness_strategy_sdk::building_blocks::{
    CrossoverAge, DirectionalChange, FittedNormalization, RollingMoments, Vwap,
};
use harness_strategy_sdk::{OutputPoint, StudyOutput, WaveletIndicator};
use serde_json::json;

#[test]
fn invalid_outputs_identify_the_exact_row_and_field() {
    let ragged = StudyOutput::Table {
        columns: vec!["signal".into()],
        rows: vec![vec![json!(1)], vec![]],
    };
    assert!(ragged.validate().unwrap_err().contains("rows[1]"));
    let nested = StudyOutput::Table {
        columns: vec!["signal".into()],
        rows: vec![vec![json!({"bad": 1})]],
    };
    let error = nested.validate().unwrap_err();
    assert!(error.contains("rows[0]") && error.contains("signal"));
    let future = StudyOutput::Series {
        points: vec![OutputPoint {
            symbol: "MSFT".into(),
            interval: "15min".into(),
            event_time_ns: 20,
            available_at_ns: 10,
            value: Some(1.0),
            undefined_reason: None,
        }],
    };
    let error = future.validate().unwrap_err();
    assert!(error.contains("points[0]") && error.contains("20") && error.contains("10"));
}

#[test]
fn finite_inputs_never_silently_produce_nonfinite_features() {
    let fit = FittedNormalization {
        mean: -f64::MAX,
        scale: 1.0,
    };
    assert!(fit.transform(f64::MAX).unwrap_err().contains("nonfinite"));
    let mut history = harness_strategy_sdk::research::WaveletHistory::default();
    assert!(history
        .observe("session", "MSFT", 1, 1, &[1e160, -1e160])
        .unwrap_err()
        .contains("nonfinite energy"));
    assert!(matches!(history.output(), StudyOutput::Table { rows, .. } if rows.is_empty()));
    let mut moments = RollingMoments::new(2).unwrap();
    assert_eq!(moments.observe(1.0).unwrap(), None);
    assert!(moments
        .observe(f64::MAX)
        .unwrap_err()
        .contains("RollingMoments"));
    assert_eq!(moments.observe(3.0).unwrap(), Some((2.0, 1.0)));
    let mut vwap = Vwap::new(2).unwrap();
    assert_eq!(vwap.observe(1.0, 1.0).unwrap(), None);
    assert!(vwap
        .observe(f64::MAX, 2.0)
        .unwrap_err()
        .contains("notional"));
    assert_eq!(vwap.observe(3.0, 1.0).unwrap(), Some(2.0));
}

#[test]
fn invalid_parameters_and_elapsed_time_report_the_offending_values() {
    let error = match WaveletIndicator::new(vec!["MSFT".into()], "15min".into(), 31, 8) {
        Ok(_) => panic!("invalid window accepted"),
        Err(error) => error,
    };
    assert!(error.contains("window=31") && error.contains("coarse_support=8"));
    let mut crossover = CrossoverAge::default();
    crossover.observe(i64::MIN, 2.0, 1.0).unwrap();
    assert!(crossover
        .observe(i64::MAX, 2.0, 1.0)
        .unwrap_err()
        .contains("overflow"));
    assert_eq!(
        crossover.observe(i64::MAX, 1.0, 2.0).unwrap(),
        (-1, Some(0), true)
    );
    let mut direction = DirectionalChange::new(1.0).unwrap();
    direction.observe(i64::MIN, 1.0).unwrap();
    direction.observe(i64::MIN + 1, 2.0).unwrap();
    assert!(direction
        .observe(i64::MAX, 1.0)
        .unwrap_err()
        .contains("overflow"));
}
