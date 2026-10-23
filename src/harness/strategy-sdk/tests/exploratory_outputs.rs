use harness_strategy_sdk::building_blocks::perceptually_important_points;
use harness_strategy_sdk::building_blocks::Atr;
use harness_strategy_sdk::building_blocks::CrossoverAge;
use harness_strategy_sdk::building_blocks::DirectionalChange;
use harness_strategy_sdk::building_blocks::Ema;
use harness_strategy_sdk::building_blocks::FittedNormalization;
use harness_strategy_sdk::building_blocks::RollingMoments;
use harness_strategy_sdk::building_blocks::Rsi;
use harness_strategy_sdk::building_blocks::Vwap;
use harness_strategy_sdk::OutputPoint;
use harness_strategy_sdk::StudyOutput;

#[test]
fn segmentation_and_shape_preserve_confirmation_and_trailing_support() {
    let mut detector = DirectionalChange::new(2.0).unwrap();
    assert!(detector.observe(10, 100.0).unwrap().is_none());
    assert!(detector.observe(20, 101.0).unwrap().is_none());
    let up = detector.observe(30, 102.0).unwrap().unwrap();
    assert_eq!(
        (up.direction, up.extreme_time_ns, up.available_at_ns),
        (1, 10, 30)
    );
    assert!(detector.observe(40, 105.0).unwrap().is_none());
    let down = detector.observe(50, 103.0).unwrap().unwrap();
    assert_eq!(
        (down.direction, down.extreme_time_ns, down.duration_ns),
        (-1, 40, Some(20))
    );
    assert!(detector.observe(49, 104.0).is_err());
    assert_eq!(
        perceptually_important_points(&[0.0, 4.0, 0.0, 0.0], 3).unwrap(),
        vec![0, 1, 3]
    );
    let mut moments = RollingMoments::new(2).unwrap();
    assert_eq!(moments.observe(1.0).unwrap(), None);
    assert_eq!(moments.observe(3.0).unwrap(), Some((2.0, 1.0)));
    assert!(moments.observe(f64::NAN).is_err());
    assert_eq!(moments.observe(5.0).unwrap(), Some((4.0, 1.0)));
}

#[test]
fn common_features_have_explicit_warmup_and_frozen_fit_behavior() {
    let mut ema = Ema::new(3).unwrap();
    assert_eq!(ema.observe(2.0).unwrap(), 2.0);
    assert_eq!(ema.observe(4.0).unwrap(), 3.0);
    let mut rsi = Rsi::new(2).unwrap();
    assert_eq!(rsi.observe(1.0).unwrap(), None);
    assert_eq!(rsi.observe(3.0).unwrap(), None);
    assert!((rsi.observe(2.0).unwrap().unwrap() - 200.0 / 3.0).abs() < 1e-12);
    let mut atr = Atr::new(2).unwrap();
    assert_eq!(atr.observe(3.0, 1.0, 2.0).unwrap(), None);
    assert_eq!(atr.observe(6.0, 4.0, 5.0).unwrap(), Some(3.0));
    assert!(atr.observe(1.0, 2.0, 3.0).is_err());
    let mut vwap = Vwap::new(2).unwrap();
    assert_eq!(vwap.observe(2.0, 1.0).unwrap(), None);
    assert_eq!(vwap.observe(4.0, 3.0).unwrap(), Some(3.5));
    let mut crossing = CrossoverAge::default();
    assert_eq!(crossing.observe(10, 2.0, 1.0).unwrap(), (1, Some(0), false));
    assert_eq!(
        crossing.observe(20, 1.0, 1.0).unwrap(),
        (1, Some(10), false)
    );
    assert_eq!(crossing.observe(30, 0.0, 1.0).unwrap(), (-1, Some(0), true));
    let fitted = FittedNormalization::fit(&[1.0, 3.0]).unwrap();
    assert_eq!(fitted.transform(4.0).unwrap(), 2.0);
    assert_eq!((fitted.mean, fitted.scale), (2.0, 1.0));
    assert!(FittedNormalization::fit(&[2.0, 2.0]).is_err());
}

#[test]
fn structured_evidence_rejects_future_values_and_unexplained_undefined_states() {
    let point = OutputPoint {
        symbol: "AAA".into(),
        interval: "1min".into(),
        event_time_ns: 10,
        available_at_ns: 20,
        value: None,
        undefined_reason: Some("warm_up".into()),
    };
    let output = StudyOutput::Series {
        points: vec![point.clone()],
    };
    output.validate().unwrap();
    let encoded = harness_strategy_sdk::serde_json::to_string(&output).unwrap();
    assert_eq!(
        harness_strategy_sdk::serde_json::from_str::<StudyOutput>(&encoded).unwrap(),
        output
    );
    let mut invalid = point.clone();
    invalid.event_time_ns = 21;
    assert!(StudyOutput::Series {
        points: vec![invalid]
    }
    .validate()
    .is_err());
    let mut invalid = point;
    invalid.undefined_reason = None;
    assert!(StudyOutput::Series {
        points: vec![invalid]
    }
    .validate()
    .is_err());
    assert!(StudyOutput::Table {
        columns: vec!["x".into()],
        rows: vec![vec![1.into(), 2.into()]]
    }
    .validate()
    .is_err());
}
