//! Resource admission and cancellation through the shared pipeline boundary.
use lake_pipeline::{Limits, Pipeline, Stage};
use std::time::Duration;

#[tokio::test]
async fn independent_budgets_backpressure_release_and_cancel_waiters() {
    let limits = Limits {
        cpu_slots: 1,
        publication_slots: 1,
        response_bytes: 24 * 1024 * 1024,
        max_response_bytes: 1024 * 1024,
        session_bytes: 1024 * 1024,
        max_session_bytes: 1024 * 1024,
        scratch_bytes: 1024,
    };
    let pipeline = Pipeline::new(limits).unwrap();
    let cpu = pipeline.cpu().await.unwrap();
    let mut response = Vec::new();
    for _ in 0..6 {
        response.push(pipeline.response().await.unwrap());
    }
    let session = pipeline.session().await.unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(5), pipeline.cpu())
            .await
            .is_err()
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(5), pipeline.response())
            .await
            .is_err()
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(5), pipeline.session())
            .await
            .is_err()
    );
    // Storage admission is independent of the saturated CPU and byte pools.
    let publication = pipeline.publication().unwrap();
    drop(cpu);
    let cpu = pipeline.cpu().await.unwrap();
    let waiting = {
        let pipeline = pipeline.clone();
        tokio::spawn(async move { pipeline.cpu().await })
    };
    pipeline.cancel();
    assert!(tokio::time::timeout(Duration::from_secs(1), waiting)
        .await
        .unwrap()
        .unwrap()
        .is_err());
    assert!(pipeline.commit(|| Ok(())).is_err());
    assert!(pipeline.check_dataset_bytes(usize::MAX).is_err());
    drop((cpu, response, session, publication));
    let snapshot = pipeline.snapshot();
    for name in ["cpu", "responses", "sessions", "publications"] {
        assert_eq!(snapshot["resources"][name]["active"], 0);
    }
    assert_eq!(snapshot["resources"]["cpu"]["peak"], 1);
    assert!(
        snapshot["resources"]["responses"]["wait_seconds"]
            .as_f64()
            .unwrap()
            >= 0.0
    );
}

#[test]
fn cancellation_fences_late_blocking_publication_and_measurements_count_work() {
    let pipeline = Pipeline::new(Limits::default()).unwrap();
    let (started, wait) = std::sync::mpsc::channel();
    let (finish, resume) = std::sync::mpsc::channel();
    let worker = {
        let pipeline = pipeline.clone();
        std::thread::spawn(move || {
            let _publication = pipeline.publication().unwrap();
            let mut timer = pipeline.timer(Stage::Upload);
            timer.bytes(4096);
            timer.rows(20);
            started.send(()).unwrap();
            resume.recv_timeout(Duration::from_secs(1)).unwrap();
            pipeline.commit::<()>(|| panic!("a late blocking publisher must never execute"))
        })
    };
    wait.recv_timeout(Duration::from_secs(1)).unwrap();
    pipeline.cancel();
    finish.send(()).unwrap();
    assert!(worker.join().unwrap().is_err());
    let snapshot = pipeline.snapshot();
    assert_eq!(snapshot["stages"]["upload"]["bytes"], 4096);
    assert_eq!(snapshot["stages"]["upload"]["rows"], 20);
    assert_eq!(snapshot["stages"]["upload"]["calls"], 1);
    assert_eq!(snapshot["resources"]["publications"]["active"], 0);
}
