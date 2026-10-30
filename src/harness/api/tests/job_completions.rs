//! Real worker notification delivery retries events, never execution or job queries.

use harness_execution::jobs::{JobAdmission, JobKind, JobRecord, JobState, ResultPublisher};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::Command;

#[test]
fn completion_delivery_retries_after_failure_and_ack_loss_without_resubmission() {
    let root = tempfile::tempdir().unwrap();
    let store = harness_execution_storage::filesystem_store(root.path());
    let mut job = JobRecord::new(
        "job-return",
        JobKind::Study,
        "accepted-digest",
        chrono::Utc::now(),
    );
    store.admit(job.clone()).unwrap();
    job.state = JobState::Failed;
    job.error_message = Some("observed failure".into());
    store.put_result(job, BTreeMap::new()).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/completions", listener.local_addr().unwrap());
    let server = std::thread::spawn(move || {
        for status in ["200 OK", "503 Service Unavailable", "204 No Content"] {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut buffer = [0u8; 8192];
            loop {
                let count = socket.read(&mut buffer).unwrap();
                assert!(count > 0);
                bytes.extend_from_slice(&buffer[..count]);
                let text = String::from_utf8_lossy(&bytes);
                if let Some(split) = text.find("\r\n\r\n") {
                    let length: usize = text[..split]
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length: ")
                                .map(str::to_owned)
                        })
                        .unwrap()
                        .parse()
                        .unwrap();
                    if bytes.len() >= split + 4 + length {
                        assert!(text[..split]
                            .to_ascii_lowercase()
                            .contains("authorization: bearer worker-secret"));
                        let event: serde_json::Value =
                            serde_json::from_slice(&bytes[split + 4..]).unwrap();
                        assert_eq!(event["job_id"], "job-return");
                        assert_eq!(event["state"], "failed");
                        assert!(event.get("accepted_input").is_none());
                        break;
                    }
                }
            }
            write!(
                socket,
                "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
        }
    });
    for attempt in 0..3 {
        let output = Command::new(env!("CARGO_BIN_EXE_harness-worker"))
            .arg("--dispatch")
            .env("HARNESS_MARKET_DATA_MODE", "local")
            .env("HARNESS_RESULTS_ROOT", root.path())
            .env("HARNESS_COMPLETION_URL", &url)
            .env("HARNESS_COMPLETION_TOKEN", "worker-secret")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            store.pending_completions().unwrap().len(),
            usize::from(attempt < 2)
        );
        assert!(store.queued().unwrap().is_empty());
    }
    server.join().unwrap();
}
