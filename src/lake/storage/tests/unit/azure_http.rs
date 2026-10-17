//! Lease behavior through the real Azure SDK; only the HTTP service is fake.

use super::*;
use azure_core::{
    headers::Headers, HttpClient, Method, Request, Response, RetryOptions, TransportOptions,
};
use bytes::Bytes;
use futures_util::{future::BoxFuture, stream};
use lake_contracts::{AssetClass, Date};

const LEASE_ID: &str = "e61c41cb-bbbd-4074-8f8e-f427e7a21077";
const NEXT_LEASE_ID: &str = "f61c41cb-bbbd-4074-8f8e-f427e7a21077";

#[derive(Debug, Default)]
struct BlobState {
    payload: Option<Vec<u8>>,
    leased: bool,
    create_race: bool,
    deny_writes: bool,
    generation: usize,
}

#[derive(Debug, Default)]
struct BlobHttp {
    state: Mutex<BlobState>,
}

fn response(status: StatusCode) -> Response {
    response_for(status, LEASE_ID)
}
fn response_for(status: StatusCode, lease: &str) -> Response {
    let mut headers = Headers::new();
    for (name, value) in [
        ("date", "Fri, 18 Oct 2024 12:00:00 GMT"),
        ("last-modified", "Fri, 18 Oct 2024 12:00:00 GMT"),
        ("etag", "\"lock\""),
        ("x-ms-request-id", LEASE_ID),
        ("x-ms-lease-id", lease),
        ("x-ms-request-server-encrypted", "true"),
    ] {
        headers.insert(name, value.to_string());
    }
    Response::new(
        status,
        headers,
        Box::pin(stream::empty::<azure_core::Result<Bytes>>()),
    )
}

impl HttpClient for BlobHttp {
    // The SDK's async-trait method uses this boxed signature.
    fn execute_request<'a, 'b, 'future>(
        &'a self,
        request: &'b Request,
    ) -> BoxFuture<'future, azure_core::Result<Response>>
    where
        'a: 'future,
        'b: 'future,
        Self: 'future,
    {
        Box::pin(async move {
            let mut state = self.state.lock().expect("blob state");
            let supplied = request.headers().get_optional_str(&"x-ms-lease-id".into());
            let expected = if state.generation <= 1 {
                LEASE_ID
            } else {
                NEXT_LEASE_ID
            };
            if request.method() == &Method::Get {
                // This is the real service's response to a ranged empty-blob
                // read. Lease acquisition must not depend on downloading it.
                return Ok(response(if state.payload.is_some() {
                    StatusCode::RequestedRangeNotSatisfiable
                } else {
                    StatusCode::NotFound
                }));
            }
            assert_eq!(request.method(), &Method::Put);
            if let Some(action) = request
                .headers()
                .get_optional_str(&"x-ms-lease-action".into())
            {
                let status = match action {
                    "acquire" if state.leased => StatusCode::Conflict,
                    "acquire" => {
                        assert!(state.payload.is_some());
                        state.leased = true;
                        state.generation += 1;
                        StatusCode::Created
                    }
                    "renew" if state.leased && supplied == Some(expected) => StatusCode::Ok,
                    "release" if state.leased && supplied == Some(expected) => {
                        state.leased = false;
                        StatusCode::Ok
                    }
                    "renew" | "release" => StatusCode::PreconditionFailed,
                    _ => panic!("unexpected lease action"),
                };
                return Ok(response_for(
                    status,
                    if state.generation <= 1 {
                        LEASE_ID
                    } else {
                        NEXT_LEASE_ID
                    },
                ));
            }
            if state.deny_writes {
                return Ok(response(StatusCode::Forbidden));
            }
            if state.create_race {
                state.payload = Some(b"another writer".to_vec());
                state.create_race = false;
            }
            let create_only =
                request.headers().get_optional_str(&"if-none-match".into()) == Some("*");
            if create_only && state.payload.is_some() {
                return Ok(response(StatusCode::PreconditionFailed));
            }
            if supplied.is_some() && (!state.leased || supplied != Some(expected)) {
                return Ok(response(StatusCode::PreconditionFailed));
            }
            if state.leased && supplied != Some(expected) {
                return Ok(response(StatusCode::Conflict));
            }
            let Body::Bytes(payload) = request.body() else {
                panic!("expected byte payload");
            };
            state.payload = Some(payload.to_vec());
            Ok(response(StatusCode::Created))
        })
    }
}

#[test]
fn manifest_writes_require_the_current_lease_and_renew_without_replacing_contents() {
    let http = Arc::new(BlobHttp::default());
    let provider =
        AzureBlobLeaseProvider::new(container(http.clone()), 15, "publications").unwrap();
    let first = provider.acquire(&scope()).unwrap();
    first.renew().unwrap();
    first.publish(b"first manifest").unwrap();
    assert_eq!(
        http.state.lock().unwrap().payload.as_deref(),
        Some(b"first manifest".as_slice())
    );
    // Expiry followed by another owner: the stale token must be rejected by the
    // same PUT that makes coverage visible, not by a prior best-effort check.
    http.state.lock().unwrap().leased = false;
    let next = provider.acquire(&scope()).unwrap();
    assert!(matches!(
        first.publish(b"stale manifest"),
        Err(DataError::Conflict(_))
    ));
    assert!(matches!(first.renew(), Err(DataError::Conflict(_))));
    next.publish(b"next manifest").unwrap();
    assert_eq!(
        http.state.lock().unwrap().payload.as_deref(),
        Some(b"next manifest".as_slice())
    );
    next.release().unwrap();
    assert!(next.publish(b"released").is_err());
}

fn container<T: HttpClient + 'static>(http: Arc<T>) -> Arc<SdkBlobContainer> {
    Arc::new(SdkBlobContainer {
        container: BlobServiceClient::builder("testaccount", StorageCredentials::anonymous())
            .transport(TransportOptions::new(http))
            .retry(RetryOptions::none())
            .container_client("lake"),
        executor: Executor::shared(),
    })
}

fn scope() -> SymbolDayScope {
    SymbolDayScope::new(
        AssetClass::Equity,
        "MSFT",
        Date::from_ymd(2026, 1, 2).unwrap(),
    )
    .unwrap()
}

#[test]
fn leases_create_once_reuse_empty_locks_and_preserve_other_writers() {
    for (payload, create_race) in [(None, false), (Some(Vec::new()), false), (None, true)] {
        let http = Arc::new(BlobHttp {
            state: Mutex::new(BlobState {
                payload: payload.clone(),
                create_race,
                ..BlobState::default()
            }),
        });
        let provider =
            AzureBlobLeaseProvider::new(container(Arc::clone(&http)), 60, "locks").unwrap();
        let lease = provider.acquire(&scope()).expect("first lease");
        assert!(matches!(
            provider.acquire(&scope()),
            Err(DataError::Conflict(_))
        ));
        lease.release().expect("release");
        lease.release().expect("idempotent release");
        provider
            .acquire(&scope())
            .expect("reacquire empty or existing lock")
            .release()
            .unwrap();
        let state = http.state.lock().unwrap();
        assert_eq!(
            state.payload.as_deref(),
            Some(if create_race {
                b"another writer".as_slice()
            } else if payload.is_none() {
                b"[]".as_slice()
            } else {
                b"".as_slice()
            })
        );
        assert!(!state.leased);
    }
}

#[test]
fn create_permission_errors_propagate_and_object_replacement_still_overwrites() {
    let http = Arc::new(BlobHttp {
        state: Mutex::new(BlobState {
            deny_writes: true,
            ..BlobState::default()
        }),
    });
    let sdk = container(Arc::clone(&http));
    let provider =
        AzureBlobLeaseProvider::new(Arc::clone(&sdk) as Arc<dyn AzureBlobContainer>, 60, "locks")
            .unwrap();
    assert!(matches!(provider.acquire(&scope()), Err(DataError::Io(_))));
    assert!(!http.state.lock().unwrap().leased);
    http.state.lock().unwrap().deny_writes = false;
    let store = AzureBlobStore::new(sdk);
    store.replace("data.parquet", b"first").unwrap();
    store.replace("data.parquet", b"second").unwrap();
    assert_eq!(
        http.state.lock().unwrap().payload.as_deref(),
        Some(b"second".as_slice())
    );
}

#[derive(Debug)]
struct ConcurrentBlobHttp(tokio::sync::Barrier);

impl HttpClient for ConcurrentBlobHttp {
    fn execute_request<'a, 'b, 'future>(
        &'a self,
        request: &'b Request,
    ) -> BoxFuture<'future, azure_core::Result<Response>>
    where
        'a: 'future,
        'b: 'future,
        Self: 'future,
    {
        Box::pin(async move {
            // Neither upload can finish until the other reaches the service.
            // The timeout makes a serial-executor regression fail, not hang.
            tokio::time::timeout(std::time::Duration::from_secs(2), self.0.wait())
                .await
                .expect("independent blob operations must overlap");
            assert_eq!(request.method(), &Method::Put);
            Ok(response(
                if request.url().path().ends_with("denied.parquet") {
                    StatusCode::Forbidden
                } else {
                    StatusCode::Created
                },
            ))
        })
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 1)]
async fn independent_uploads_overlap_and_keep_their_own_results() {
    let store = Arc::new(AzureBlobStore::new(container(Arc::new(
        ConcurrentBlobHttp(tokio::sync::Barrier::new(2)),
    ))));
    let good_store = store.clone();
    let good = tokio::spawn(async move { good_store.replace("good.parquet", b"good") });
    let denied = tokio::spawn(async move { store.replace("denied.parquet", b"denied") });
    good.await.unwrap().expect("unrelated upload succeeds");
    assert!(matches!(denied.await.unwrap(), Err(DataError::Io(_))));
}

#[derive(Debug)]
struct MetadataHttp {
    active: std::sync::atomic::AtomicUsize,
    peak: std::sync::atomic::AtomicUsize,
    wave: tokio::sync::Barrier,
}
impl HttpClient for MetadataHttp {
    fn execute_request<'a, 'b, 'future>(
        &'a self,
        request: &'b Request,
    ) -> BoxFuture<'future, azure_core::Result<Response>>
    where
        'a: 'future,
        'b: 'future,
        Self: 'future,
    {
        Box::pin(async move {
            use std::sync::atomic::Ordering;
            assert_eq!(request.method(), &Method::Get);
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.peak.fetch_max(active, Ordering::SeqCst);
            tokio::time::timeout(std::time::Duration::from_secs(2), self.wave.wait())
                .await
                .expect("metadata reads must overlap");
            let payload = Bytes::from(request.url().path().rsplit('/').next().unwrap().to_string());
            let mut headers = Headers::new();
            for (name, value) in [
                ("date", "Fri, 18 Oct 2024 12:00:00 GMT"),
                ("last-modified", "Fri, 18 Oct 2024 12:00:00 GMT"),
                ("x-ms-creation-time", "Fri, 18 Oct 2024 12:00:00 GMT"),
                ("etag", "\"metadata\""),
                ("x-ms-request-id", LEASE_ID),
                ("x-ms-blob-type", "BlockBlob"),
                ("x-ms-server-encrypted", "true"),
            ] {
                headers.insert(name, value.to_string());
            }
            headers.insert("content-length", payload.len().to_string());
            self.active.fetch_sub(1, Ordering::SeqCst);
            Ok(Response::new(
                StatusCode::Ok,
                headers,
                Box::pin(stream::once(async move { Ok(payload) })),
            ))
        })
    }
}
#[test]
fn long_history_metadata_downloads_overlap_with_bounded_fanout_and_ordered_results() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let http = Arc::new(MetadataHttp {
        active: AtomicUsize::new(0),
        peak: AtomicUsize::new(0),
        wave: tokio::sync::Barrier::new(32),
    });
    let objects = AzureReadableBlobStore::new(container(http.clone()));
    let paths: Vec<_> = (0..64).map(|i| format!("manifest-{i:02}.json")).collect();
    let values = objects.read_many(&paths).unwrap();
    assert_eq!(
        values,
        paths
            .iter()
            .map(|p| p.as_bytes().to_vec())
            .collect::<Vec<_>>()
    );
    assert!(http.peak.load(Ordering::SeqCst) > 1);
    assert!(http.peak.load(Ordering::SeqCst) <= 32);
    assert_eq!(http.active.load(Ordering::SeqCst), 0);
}
