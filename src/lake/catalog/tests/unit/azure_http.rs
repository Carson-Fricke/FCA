//! Table concurrency through the real Azure SDK; only HTTP is simulated.

use super::*;
use azure_core::{
    headers::Headers, HttpClient, Method, Request, Response, RetryOptions, StatusCode,
    TransportOptions,
};
use azure_data_tables::clients::TableServiceClientBuilder;
use azure_storage::StorageCredentials;
use bytes::Bytes;
use futures_util::{future::BoxFuture, stream};

#[derive(Debug)]
struct ConcurrentTableHttp(tokio::sync::Barrier);

impl HttpClient for ConcurrentTableHttp {
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
            // A serial executor cannot satisfy this rendezvous. Fail within
            // two seconds rather than leave synchronous callers hung forever.
            tokio::time::timeout(std::time::Duration::from_secs(2), self.0.wait())
                .await
                .expect("independent table operations must overlap");
            assert_eq!(request.method(), &Method::Put);
            let mut headers = Headers::new();
            headers.insert("date", "Fri, 18 Oct 2024 12:00:00 GMT");
            headers.insert("etag", "\"entity\"");
            headers.insert("x-ms-request-id", "e61c41cb-bbbd-4074-8f8e-f427e7a21077");
            headers.insert("x-ms-version", "2019-02-02");
            headers.insert("server", "Windows-Azure-Table/1.0");
            Ok(Response::new(
                if request.url().path().contains("denied") {
                    StatusCode::Forbidden
                } else {
                    StatusCode::NoContent
                },
                headers,
                Box::pin(stream::empty::<azure_core::Result<Bytes>>()),
            ))
        })
    }
}

#[test]
fn independent_upserts_overlap_and_keep_their_own_results() {
    let store = SdkTableEntityStore {
        table: TableServiceClientBuilder::new("testaccount", StorageCredentials::anonymous())
            .transport(TransportOptions::new(Arc::new(ConcurrentTableHttp(
                tokio::sync::Barrier::new(2),
            ))))
            .retry(RetryOptions::none())
            .build()
            .table_client("coverage"),
        executor: Executor::shared(),
    };
    let entity = |key| {
        serde_json::json!({"PartitionKey": "coverage", "RowKey": key})
            .as_object()
            .unwrap()
            .clone()
    };
    std::thread::scope(|threads| {
        let good = threads.spawn(|| store.upsert_entity(entity("good")));
        let denied = threads.spawn(|| store.upsert_entity(entity("denied")));
        good.join().unwrap().expect("unrelated upsert succeeds");
        assert!(matches!(denied.join().unwrap(), Err(DataError::Io(_))));
    });
}
