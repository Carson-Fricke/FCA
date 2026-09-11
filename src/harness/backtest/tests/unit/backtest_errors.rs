use super::*;

#[test]
fn policies_round_trip() {
    for value in [AbortPolicy::Raise, AbortPolicy::Stop] {
        assert_eq!(AbortPolicy::parse(value.value()), Some(value));
    }
    for value in [
        StrategyHaltPolicy::HaltStrategy,
        StrategyHaltPolicy::AbortRun,
        StrategyHaltPolicy::Raise,
    ] {
        assert_eq!(StrategyHaltPolicy::parse(value.value()), Some(value));
    }
    for value in [
        TeardownPolicy::Always,
        TeardownPolicy::OnSuccess,
        TeardownPolicy::Never,
    ] {
        assert_eq!(TeardownPolicy::parse(value.value()), Some(value));
    }
    for value in [
        FillModel::SimpleTouch,
        FillModel::LatencySize,
        FillModel::TradeThrough,
    ] {
        assert_eq!(FillModel::parse(value.value()), Some(value));
    }
}

#[test]
fn defaults_match_python() {
    assert_eq!(AbortPolicy::default(), AbortPolicy::Raise);
    assert_eq!(
        StrategyHaltPolicy::default(),
        StrategyHaltPolicy::HaltStrategy
    );
    assert_eq!(TeardownPolicy::default(), TeardownPolicy::Always);
    assert_eq!(FillModel::default(), FillModel::SimpleTouch);
}
