//! Managed wavelet observations through real dependency-first market routing.
use harness_strategy_sdk::{
    route_bar, BarEvent, BarFrame, Decimal, Indicator, IndicatorMissing, MarketConsumer,
    ObservationContext, Subscription, WaveletIndicator, WaveletSnapshot,
};
use std::collections::BTreeMap;

struct Owner {
    wavelet: WaveletIndicator,
    decisions: Vec<Option<WaveletSnapshot>>,
}
impl MarketConsumer for Owner {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Bars {
            symbols: vec!["AAA".into()],
            interval: "1min".into(),
        }]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.wavelet]
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        self.decisions
            .push(self.wavelet.snapshot_at("AAA", frame.available_at_ns));
    }
}
fn owner() -> Owner {
    Owner {
        wavelet: WaveletIndicator::new(vec!["AAA".into(), "BBB".into()], "1d".into(), 4, 4)
            .unwrap(),
        decisions: vec![],
    }
}
fn frame(
    interval: &'static str,
    start: i64,
    available: i64,
    prices: &[(&'static str, i128)],
    missing: Vec<&'static str>,
) -> BarFrame<'static> {
    BarFrame {
        interval,
        interval_start_ns: start,
        interval_end_ns: start + 10,
        available_at_ns: available,
        bars_by_symbol: prices
            .iter()
            .map(|(symbol, price)| {
                (
                    *symbol,
                    BarEvent {
                        event_id: "bar",
                        sequence: 1,
                        timestamp_ns: start,
                        symbol,
                        open: Decimal::from(*price),
                        high: Decimal::from(*price),
                        low: Decimal::from(*price),
                        close: Decimal::from(*price),
                        volume: Decimal::from(1),
                    },
                )
            })
            .collect::<BTreeMap<_, _>>(),
        missing_symbols: missing,
    }
}
fn daily(owner: &mut Owner, index: i64, price: i128) {
    route_bar(
        owner,
        &frame(
            "1d",
            index * 20,
            index * 20 + 12,
            &[("AAA", price), ("BBB", 10)],
            vec![],
        ),
        &mut ObservationContext,
    )
    .unwrap();
}
#[test]
fn completed_slow_bars_are_available_only_at_their_boundary_and_snapshots_do_not_relabel() {
    let mut owner = owner();
    for (index, price) in [1, 1, 3].into_iter().enumerate() {
        daily(&mut owner, index as i64, price);
    }
    assert!(owner.wavelet.snapshot_at("AAA", 51).is_none());
    // An intraday trigger before the last slow bar is available cannot see it.
    route_bar(
        &mut owner,
        &frame("1min", 51, 61, &[("AAA", 2)], vec![]),
        &mut ObservationContext,
    )
    .unwrap();
    assert!(owner.decisions.last().unwrap().is_none());
    daily(&mut owner, 3, 3);
    route_bar(
        &mut owner,
        &frame("1min", 72, 82, &[("AAA", 3)], vec![]),
        &mut ObservationContext,
    )
    .unwrap();
    let frozen = owner.decisions.last().unwrap().clone().unwrap();
    assert_eq!(frozen.available_at_ns(), 72);
    assert_eq!(frozen.decision_time_ns(), 82);
    assert_eq!(frozen.window_start_ns(), 0);
    assert_eq!(frozen.coarse_energy_share(), Decimal::from(1));
    assert_eq!(frozen.trend_bps(), Decimal::from(10000));
    assert_eq!(frozen.scales()[0].energy(), Decimal::ZERO);
    assert_eq!(frozen.scales()[1].energy(), Decimal::from(4));
    assert!(owner.wavelet.snapshot_at("BBB", 82).is_none()); // flat, independent symbol
    daily(&mut owner, 5, 1000);
    assert_eq!(owner.wavelet.snapshot_at("AAA", 82), Some(frozen.clone()));
    assert_eq!(owner.decisions[1], Some(frozen));
    assert_ne!(
        owner.wavelet.snapshot_at("AAA", 112),
        owner.wavelet.snapshot_at("AAA", 82)
    );
}
#[test]
fn prefix_and_future_perturbation_leave_every_historical_snapshot_identical() {
    let mut left = owner();
    let mut right = owner();
    for (i, price) in [10, 11, 13, 12, 15, 18].into_iter().enumerate() {
        daily(&mut left, i as i64, price);
        daily(&mut right, i as i64, price);
    }
    let prefix: Vec<_> = (0..=112)
        .map(|time| left.wavelet.snapshot_at("AAA", time))
        .collect();
    for i in 6..12 {
        daily(&mut left, i, 20 + i as i128);
        daily(&mut right, i, 10000 - i as i128);
    }
    for time in 0..=112 {
        assert_eq!(left.wavelet.snapshot_at("AAA", time), prefix[time as usize]);
        assert_eq!(
            right.wavelet.snapshot_at("AAA", time),
            prefix[time as usize]
        );
    }
}
#[test]
fn missing_input_resets_warmup_and_never_carries_previous_features() {
    let mut owner = owner();
    for (i, p) in [1, 1, 3, 3].into_iter().enumerate() {
        daily(&mut owner, i as i64, p);
    }
    let before = owner.wavelet.snapshot_at("AAA", 72).unwrap();
    route_bar(
        &mut owner,
        &frame("1d", 80, 92, &[("BBB", 10)], vec!["AAA"]),
        &mut ObservationContext,
    )
    .unwrap();
    assert!(owner.wavelet.snapshot_at("AAA", 92).is_none());
    for (i, p) in [(5, 2), (6, 3), (7, 4)] {
        daily(&mut owner, i, p);
        assert!(owner.wavelet.snapshot_at("AAA", i * 20 + 12).is_none());
    }
    daily(&mut owner, 8, 5);
    assert!(owner.wavelet.snapshot_at("AAA", 172).is_some());
    assert_eq!(owner.wavelet.snapshot_at("AAA", 72), Some(before));
    let sequence = &owner.wavelet.sequence().completed()[&("AAA".into(), "1d".into())];
    assert_eq!(sequence[4].missing, Some(IndicatorMissing::MissingInput));
    assert_eq!(sequence[5].missing, Some(IndicatorMissing::Undefined));
}
#[test]
fn configuration_rejects_noncausal_or_unbounded_window_shapes() {
    for (window, support) in [(0, 2), (3, 2), (8192, 2), (4, 3), (4, 8)] {
        assert!(WaveletIndicator::new(vec!["AAA".into()], "1d".into(), window, support).is_err());
    }
    assert!(WaveletIndicator::new(vec![], "1d".into(), 4, 2).is_err());
}
