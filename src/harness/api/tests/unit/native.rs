use super::*;

#[test]
fn data_dir_no_longer_short_circuits() {
    let runner = NativeBacktestJobRunner;
    let args = BacktestJobArgs {
        config: Some(PathBuf::from("does-not-exist.yaml")),
        data_dir: Some(PathBuf::from("lake")),
        output_dir: None,
    };
    match runner.run(&args) {
        Err(BacktestJobError::Engine(reason)) => {
            assert!(!reason.contains("does not yet read"), "{reason}");
        }
        other => panic!("expected an engine error, got {other:?}"),
    }
}

#[test]
fn missing_configuration_is_reported() {
    let runner = NativeBacktestJobRunner;
    match runner.run(&BacktestJobArgs::default()) {
        Err(BacktestJobError::Engine(reason)) => {
            assert!(reason.contains("--config"), "{reason}");
        }
        other => panic!("expected an engine error, got {other:?}"),
    }
}
