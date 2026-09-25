use std::sync::Arc;
use std::time::Duration;

use super::*;

fn instant_completions() -> Vec<Duration> {
    vec![Duration::from_millis(10); 8]
}

#[test]
fn rising_throughput_ramps_the_bound() {
    let lane = Lane::with_tuning(None, 256, LaneTuning::default());
    assert_eq!(lane.bound(), 32);
    // First window settles near Little's law: 10 pages/s x 10ms = 0.1 -> 2.
    lane.observe_window(10, Duration::from_secs(1), &instant_completions());
    let settled = lane.bound();
    assert_eq!(settled, 2);
    // Double the rate: ramping is direct evidence of headroom.
    lane.observe_window(20, Duration::from_secs(1), &instant_completions());
    assert!(lane.bound() > settled, "rising throughput must ramp");
    let ramped = lane.bound();
    lane.observe_window(40, Duration::from_secs(1), &instant_completions());
    assert!(lane.bound() > ramped, "sustained growth keeps ramping");
}

#[test]
fn an_inflated_latency_window_does_not_inflate_the_bound() {
    // The settle target is t_best x min-latency. Using the current (inflated)
    // p50 there would feed queueing back into a larger bound; the bound must
    // stay at the bandwidth-delay product instead.
    let lane = Lane::with_tuning(None, 512, LaneTuning::default());
    lane.observe_window(100, Duration::from_secs(1), &instant_completions());
    let base = lane.bound();
    let slow = vec![Duration::from_secs(5); 8];
    lane.observe_window(100, Duration::from_secs(1), &slow);
    assert_eq!(
        lane.bound(),
        base,
        "min-latency keeps the bandwidth-delay product stable"
    );
}

#[test]
fn uniform_saturation_beyond_the_ceiling_backs_the_bound_off() {
    let lane = Lane::with_tuning(None, 256, LaneTuning::default());
    lane.observe_window(100, Duration::from_secs(1), &instant_completions());
    lane.observe_window(200, Duration::from_secs(1), &instant_completions());
    let before = lane.bound();
    // Every request slows down together past the absolute ceiling.
    let saturated = vec![Duration::from_secs(12); 8];
    lane.observe_window(8, Duration::from_secs(1), &saturated);
    assert!(
        lane.bound() < before,
        "uniform saturation must back off: {before} -> {}",
        lane.bound()
    );
}

#[test]
fn a_governor_bound_window_holds_at_littles_law() {
    // 6000 rpm = 100 req/s; serving 200/s means the governor is the wall.
    let lane = Lane::with_tuning(Some(6000), 1024, LaneTuning::default());
    let completions = vec![Duration::from_millis(500); 8];
    lane.observe_window(200, Duration::from_secs(1), &completions);
    // 1.1 x 100/s x 0.5s = 55 in flight.
    assert_eq!(lane.bound(), 55);
}

#[test]
fn the_bound_never_exceeds_the_ceiling() {
    let lane = Lane::with_tuning(None, 64, LaneTuning::default());
    for rate in [1_000u32, 10_000, 100_000] {
        lane.observe_window(rate, Duration::from_secs(1), &instant_completions());
    }
    let bound = lane.bound();
    assert!(
        (2..=64).contains(&bound),
        "bound {bound} must stay in [2, 64]"
    );
}

#[tokio::test]
async fn the_limiter_blocks_at_the_bound_and_wakes_on_release() {
    let tuning = LaneTuning {
        initial_bound: 2,
        ..LaneTuning::default()
    };
    let lane = Arc::new(Lane::with_tuning(None, 2, tuning));
    let first = lane.acquire().await;
    let second = lane.acquire().await;
    let waiter = {
        let lane = Arc::clone(&lane);
        tokio::spawn(async move {
            let _permit = lane.acquire().await;
            7
        })
    };
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert!(
        !waiter.is_finished(),
        "third acquire must block at the bound"
    );
    drop(first);
    let joined = tokio::time::timeout(Duration::from_secs(2), waiter)
        .await
        .expect("waiter wakes after a release")
        .expect("join");
    assert_eq!(joined, 7);
    drop(second);
}

#[test]
fn lanes_are_shared_per_api_key() {
    let first = lane_for(Some("shared-key-test"), None, 64);
    let second = lane_for(Some("shared-key-test"), None, 64);
    assert!(Arc::ptr_eq(&first, &second));
    let other = lane_for(Some("other-key-test"), None, 64);
    assert!(!Arc::ptr_eq(&first, &other));
}
