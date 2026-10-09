use std::path::PathBuf;
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use api::local::{NativeBacktestSubmitter, NativeScreeningSubmitter, NativeStudySubmitter};
use mcp::store::{FileSystemJobStore, InMemoryJobStore, JobState, JobStore, StoreError};
use mcp::tools::{BacktestSubmitter, ScreeningSubmitter, StudySubmitter};
use serde_json::{json, Value};

fn write_temp(name: &str, document: &Value) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-job-id-receipt-{}-{name}.json",
        std::process::id()
    ));
    std::fs::write(&path, document.to_string()).unwrap();
    path
}

fn replay_document(shell: &str) -> Value {
    let subscriptions = if shell == "study" {
        json!([{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}])
    } else {
        json!([{"data_type": "quote", "symbols": ["SPY"]}])
    };
    json!({
        "schema_version": 1,
        "shell": shell,
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-04T21:00:00Z",
        "data": {"symbols": ["SPY"], "subscriptions": subscriptions}
    })
}

fn screen_document() -> Value {
    let source = "// screening source\n";
    let digest = backtest::sha256::digest_hex(source.as_bytes());
    json!({
        "schema_version": 1,
        "shell": "screen",
        "name": "job-id-receipt",
        "as_of": "2024-01-19",
        "bar_intervals": ["1d"],
        "lookback_days": 20,
        "rank_metric": "close",
        "source": source,
        "bundle_hash": digest,
        "definition_id": format!("sha256:{}", "0".repeat(64)),
        "definition_hash": "0".repeat(64)
    })
}

#[test]
fn backtest_receipt_echoes_the_submitted_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeBacktestSubmitter::new(store.clone());
    let path = write_temp("backtest", &replay_document("backtest"));
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-receipt-backtest"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-receipt-backtest");
    assert_eq!(receipt["state"], "queued");
    assert_eq!(receipt["kind"], "backtest");
    assert_eq!(
        store.get("job-receipt-backtest").unwrap().job_id,
        "job-receipt-backtest"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn study_receipt_echoes_the_submitted_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeStudySubmitter::new(store.clone());
    let path = write_temp("study", &replay_document("study"));
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-receipt-study"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-receipt-study");
    assert_eq!(receipt["kind"], "study");
    assert_eq!(
        store.get("job-receipt-study").unwrap().job_id,
        "job-receipt-study"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn screen_receipt_echoes_the_submitted_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeScreeningSubmitter::new(
        store.clone(),
        Arc::new(api::http::screener::UnavailableScreeningRunner),
    );
    let path = write_temp("screen", &screen_document());
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-receipt-screen"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-receipt-screen");
    assert_eq!(receipt["kind"], "screen");
    assert_eq!(
        store.get("job-receipt-screen").unwrap().job_id,
        "job-receipt-screen"
    );
    std::fs::remove_file(path).unwrap();
}

#[test]
fn submitted_job_id_overrides_a_document_job_id() {
    let store = Arc::new(InMemoryJobStore::new());
    let submitter = NativeBacktestSubmitter::new(store.clone());
    let mut document = replay_document("backtest");
    document["job_id"] = json!("job-document");
    let path = write_temp("backtest-overlay", &document);
    let receipt = submitter
        .submit(path.to_str().unwrap(), None, Some("job-submitted"))
        .unwrap();
    assert_eq!(receipt["job_id"], "job-submitted");
    assert!(store.get("job-document").is_none());
    assert_eq!(store.get("job-submitted").unwrap().job_id, "job-submitted");
    std::fs::remove_file(path).unwrap();
}

/// Keep the external execution boundary busy while querying its durable job.
struct WaitingScreenRunner {
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}

impl api::http::screener::ScreeningRunner for WaitingScreenRunner {
    fn run(
        &self,
        _configuration: &backtest::backtest::screen::ScreenConfig,
        _output_dir: Option<&std::path::Path>,
    ) -> Result<api::http::screener::ScreenRunOutcome, String> {
        self.entered.send(()).unwrap();
        self.release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(2))
            .unwrap();
        Err("observed runner failure".to_string())
    }
}

#[test]
fn active_local_worker_is_durably_running_and_duplicate_submission_joins() {
    let root = std::env::temp_dir().join(format!("harness-worker-state-{}", std::process::id()));
    let store = Arc::new(FileSystemJobStore::new(&root));
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let submitter = NativeScreeningSubmitter::new(
        store.clone(),
        Arc::new(WaitingScreenRunner {
            entered: entered_tx,
            release: Mutex::new(release_rx),
        }),
    );
    let job_id = "job-active-worker";
    let path = write_temp("active-worker", &screen_document());
    submitter
        .submit(path.to_str().unwrap(), None, Some(job_id))
        .unwrap();
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    // A separately opened reader observes the same running state and identity.
    let reader = FileSystemJobStore::new(&root);
    let running = reader.get(job_id).unwrap();
    assert_eq!(running.state, JobState::Running);
    assert!(running.updated_at >= running.created_at);
    assert_eq!(
        store.mark_running(job_id, chrono::Utc::now()).unwrap(),
        running
    );
    let joined = submitter
        .submit(path.to_str().unwrap(), None, Some(job_id))
        .unwrap();
    assert_eq!(joined["state"], "running");
    assert_eq!(joined["job_id"], job_id);
    assert_eq!(reader.get(job_id).unwrap(), running);
    release_tx.send(()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let terminal = loop {
        if let Some(record) = reader.get(job_id) {
            if record.state.is_terminal() {
                break record;
            }
        }
        assert!(Instant::now() < deadline, "worker did not settle");
        std::thread::sleep(Duration::from_millis(1));
    };
    assert_eq!(terminal.state, JobState::Failed);
    assert_eq!(
        terminal.error_message.as_deref(),
        Some("observed runner failure")
    );
    assert_eq!(terminal.input_digest, running.input_digest);
    assert_eq!(terminal.accepted_input, running.accepted_input);
    assert!(matches!(
        store.mark_running(job_id, chrono::Utc::now()),
        Err(StoreError::Terminal(_))
    ));
    assert_eq!(reader.get(job_id).unwrap(), terminal);
    std::fs::remove_file(path).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}
