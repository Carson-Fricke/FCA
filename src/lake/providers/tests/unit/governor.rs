use super::*;

#[tokio::test(start_paused = true)]
async fn sixty_rpm_spaces_acquires_one_second_apart() {
    let governor = RequestGovernor::new(Some(60));
    assert_eq!(governor.requests_per_minute(), Some(60));
    let start = Instant::now();
    governor.acquire().await;
    assert_eq!(
        start.elapsed(),
        Duration::ZERO,
        "first acquire is immediate"
    );
    governor.acquire().await;
    assert_eq!(start.elapsed(), Duration::from_secs(1));
    governor.acquire().await;
    assert_eq!(start.elapsed(), Duration::from_secs(2));
}

#[tokio::test(start_paused = true)]
async fn disabled_governor_never_waits() {
    let governor = RequestGovernor::new(None);
    assert_eq!(governor.requests_per_minute(), None);
    let start = Instant::now();
    for _ in 0..10 {
        governor.acquire().await;
    }
    assert_eq!(start.elapsed(), Duration::ZERO);
}

#[tokio::test(start_paused = true)]
async fn zero_budget_is_a_no_op() {
    let governor = RequestGovernor::new(Some(0));
    assert_eq!(governor.requests_per_minute(), Some(0));
    let start = Instant::now();
    for _ in 0..10 {
        governor.acquire().await;
    }
    assert_eq!(start.elapsed(), Duration::ZERO);
}

#[tokio::test(start_paused = true)]
async fn concurrent_acquires_get_distinct_slots() {
    let governor = RequestGovernor::new(Some(120));
    let start = Instant::now();
    let first = governor.clone();
    let second = governor.clone();
    let (left, right) = tokio::join!(async move { first.acquire().await }, async move {
        second.acquire().await
    });
    let _ = (left, right);
    assert_eq!(start.elapsed(), Duration::from_millis(500));
}
