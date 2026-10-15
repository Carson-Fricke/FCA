//! Lease behavior through the real Azure SDK; only the HTTP service is fake.

use super::*;
use azure_core::{
    headers::Headers, HttpClient, Method, Request, Response, RetryOptions, TransportOptions,
};
use bytes::Bytes;
use futures_util::{future::BoxFuture, stream};
use lake_contracts::{AssetClass, Date};

const LEASE_ID: &str = "e61c41cb-bbbd-4074-8f8e-f427e7a21077";

#[derive(Debug, Default)]
struct BlobState {
    payload: Option<Vec<u8>>,
    leased: bool,
    create_race: bool,
    deny_writes: bool,
}

#[derive(Debug, Default)]
struct BlobHttp {
    state: Mutex<BlobState>,
}

fn response(status: StatusCode) -> Response {
    let mut headers = Headers::new();
    for (name, value) in [
        ("date", "Fri, 18 Oct 2024 12:00:00 GMT"),
        ("last-modified", "Fri, 18 Oct 2024 12:00:00 GMT"),
        ("etag", "\"lock\""),
        ("x-ms-request-id", LEASE_ID),
        ("x-ms-lease-id", LEASE_ID),
        ("x-ms-request-server-encrypted", "true"),
    ] {
        headers.insert(name, value);
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
                        StatusCode::Created
                    }
                    "release" => {
                        state.leased = false;
                        StatusCode::Ok
                    }
                    _ => panic!("unexpected lease action"),
                };
                return Ok(response(status));
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
            if state.leased {
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
                payload,
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
