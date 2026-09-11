use super::*;

#[test]
fn defaults_match_python() {
    let config = RecorderConfig::new("run", "out").expect("config");
    assert_eq!(config.seed, Seed::Text("harness-run-recorder".to_string()));
    assert_eq!(config.batch_size, 4096);
    assert!(!config.record_events);
    assert!(config.record_payloads);
    assert_eq!(config.compression, "zstd");
    assert_eq!(config.parquet_version, "2.6");
}

#[test]
fn blank_run_id_is_rejected() {
    assert_eq!(
        RecorderConfig::new("   ", "out").err(),
        Some(ConfigError::EmptyString("run_id"))
    );
}

#[test]
fn zero_batch_size_is_rejected() {
    let config = RecorderConfig::new("run", "out").expect("config");
    assert_eq!(
        config.with_batch_size(0).err(),
        Some(ConfigError::BatchSize)
    );
}

#[test]
fn seed_text_encoding() {
    assert_eq!(Seed::Int(-7).as_text(), "-7");
    assert_eq!(Seed::Bytes(vec![0x00, 0xab, 0xff]).as_text(), "00abff");
    assert_eq!(Seed::Text("x".to_string()).as_text(), "x");
}

#[test]
fn enum_values_round_trip() {
    for kind in [
        ActionKind::Submit,
        ActionKind::Cancel,
        ActionKind::Replace,
        ActionKind::Hold,
        ActionKind::Custom,
    ] {
        assert_eq!(ActionKind::parse(kind.value()), Some(kind));
    }
    for reason in [
        TransitionReason::Fill,
        TransitionReason::Mark,
        TransitionReason::Snapshot,
        TransitionReason::Close,
    ] {
        assert_eq!(TransitionReason::parse(reason.value()), Some(reason));
    }
    for scope in [
        MarkerScope::Run,
        MarkerScope::State,
        MarkerScope::Position,
        MarkerScope::Note,
    ] {
        assert_eq!(MarkerScope::parse(scope.value()), Some(scope));
    }
}

#[test]
fn fixed_clock_reports_its_timestamp() {
    let clock = FixedClock::new(42);
    assert_eq!(clock.now_ns(), 42);
}
