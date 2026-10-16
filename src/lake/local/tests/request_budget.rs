//! Independent local workers share one OS-clock ledger, including reboot fencing.
use lake_ingest_jobs::{BudgetState, JobPersistence};
use lake_local::LocalJobStore;

#[tokio::test]
async fn independent_local_workers_share_the_hard_cap_and_persist_a_reboot_hold() {
    let directory = tempfile::tempdir().unwrap();
    let first = LocalJobStore::new(directory.path().into());
    let second = LocalJobStore::new(directory.path().into());
    let scope = "d".repeat(64);
    let (a, b) = tokio::join!(
        first.reserve_requests(&scope, 10000, 6000),
        second.reserve_requests(&scope, 10000, 6000)
    );
    let counts = [a.unwrap().count, b.unwrap().count];
    assert_eq!(counts.iter().sum::<u32>(), 10000);
    assert!(counts.contains(&6000) && counts.contains(&4000));

    let path = directory
        .path()
        .join(format!("ingestion/budgets/{scope}.json"));
    let mut prior_boot = BudgetState::new(10000).unwrap();
    prior_boot.reserve(10000, 10000, i64::MAX / 2).unwrap();
    std::fs::write(&path, serde_json::to_vec(&prior_boot).unwrap()).unwrap();
    assert_eq!(
        first
            .reserve_requests(&scope, 10000, 1)
            .await
            .unwrap()
            .count,
        0
    );
    let held: BudgetState = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert!(held.observed_at < prior_boot.observed_at);
    assert_eq!(
        held.charges.iter().map(|charge| charge.count).sum::<u32>(),
        10000
    );
    assert_eq!(
        second
            .reserve_requests(&scope, 10000, 1)
            .await
            .unwrap()
            .count,
        0
    );
}
