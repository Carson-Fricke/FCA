//! Execution-scoped manifest pinning and an unauthenticated signed-object transport.

use crate::{client::LakeClient, error::LakeClientError, spec::ReadSpec};
use harness_market_reader::{MarketDataSnapshot, Partition, PartitionSource, ReaderError};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResolvedSnapshot {
    pub snapshot: MarketDataSnapshot,
    pub locations: std::sync::Mutex<BTreeMap<String, String>>,
}

pub(crate) type Pins = Arc<Mutex<BTreeMap<String, Arc<tokio::sync::OnceCell<ResolvedSnapshot>>>>>;

impl LakeClient {
    pub(crate) async fn resolve_snapshot(
        &self,
        spec: &ReadSpec,
    ) -> Result<Arc<tokio::sync::OnceCell<ResolvedSnapshot>>, LakeClientError> {
        let key = serde_json::to_string(&spec.query_params())
            .map_err(|e| LakeClientError::Protocol(e.to_string()))?;
        let pin = self
            .pins
            .lock()
            .map_err(|_| LakeClientError::Protocol("snapshot lock poisoned".into()))?
            .entry(key)
            .or_default()
            .clone();
        pin.get_or_try_init(|| async {
            if let Some(access) = &self.access {
                return Ok(ResolvedSnapshot {
                    snapshot: access.snapshot(spec).await?,
                    locations: Default::default(),
                });
            }
            let response = self
                .http
                .get(self.url("/v1/snapshot"))
                .query(&spec.query_params())
                .send()
                .await
                .map_err(|e| LakeClientError::Transport(e.to_string()))?;
            let status = response.status();
            let payload = response
                .bytes()
                .await
                .map_err(|e| LakeClientError::Transport(e.to_string()))?;
            if !status.is_success() {
                return Err(super::client::http_error(status.as_u16(), &payload));
            }
            let resolved: ResolvedSnapshot = serde_json::from_slice(&payload)
                .map_err(|e| LakeClientError::Decode(e.to_string()))?;
            resolved
                .snapshot
                .validate()
                .map_err(|e| LakeClientError::Protocol(e.to_string()))?;
            let request = &resolved.snapshot.request;
            if request.asset_class != spec.asset_class
                || request.data_type != spec.data_type
                || request.symbol != spec.symbol.trim().to_uppercase()
                || request.start_date != spec.start_date
                || request.end_date != spec.end_date
                || request.interval != spec.interval
            {
                return Err(LakeClientError::Protocol(
                    "snapshot acknowledgement does not match the requested window".into(),
                ));
            }
            Ok(resolved)
        })
        .await?;
        Ok(pin)
    }
}

pub(crate) struct SignedObjects<'a> {
    pub observed: std::sync::atomic::AtomicU64,
    pub http: &'a reqwest::Client,
    pub control: &'a LakeClient,
    pub snapshot: &'a MarketDataSnapshot,
    pub locations: &'a std::sync::Mutex<BTreeMap<String, String>>,
}

impl PartitionSource for SignedObjects<'_> {
    fn read<'a>(
        &'a self,
        partition: &'a Partition,
    ) -> futures_util::future::BoxFuture<'a, Result<bytes::Bytes, ReaderError>> {
        Box::pin(async move {
            let location = self
                .locations
                .lock()
                .map_err(|_| ReaderError::Source("location lock poisoned".into()))?
                .get(&partition.checksum)
                .cloned()
                .ok_or_else(|| ReaderError::Source("missing object location".into()))?;
            // Never send the API bearer token to object storage, including redirects.
            let mut response = self
                .http
                .get(location)
                .send()
                .await
                .map_err(|_| ReaderError::Source("object request failed".into()))?;
            if matches!(response.status().as_u16(), 401 | 403) {
                let renewed = self
                    .control
                    .http
                    .post(self.control.url("/v1/snapshot/locations"))
                    .json(self.snapshot)
                    .send()
                    .await
                    .map_err(|_| ReaderError::Source("location renewal failed".into()))?;
                if !renewed.status().is_success() {
                    return Err(ReaderError::Source(format!(
                        "location renewal returned {}",
                        renewed.status()
                    )));
                }
                let locations: BTreeMap<String, String> = renewed
                    .json()
                    .await
                    .map_err(|_| ReaderError::Source("invalid location acknowledgement".into()))?;
                let location = locations
                    .get(&partition.checksum)
                    .ok_or_else(|| ReaderError::Source("renewal omitted pinned partition".into()))?
                    .clone();
                *self
                    .locations
                    .lock()
                    .map_err(|_| ReaderError::Source("location lock poisoned".into()))? = locations;
                response = self
                    .http
                    .get(location)
                    .send()
                    .await
                    .map_err(|_| ReaderError::Source("renewed object request failed".into()))?;
            }
            if !response.status().is_success() {
                return Err(ReaderError::Source(format!(
                    "object returned {}",
                    response.status()
                )));
            }
            let mut payload = bytes::BytesMut::new();
            while let Some(chunk) = response
                .chunk()
                .await
                .map_err(|_| ReaderError::Source("object response interrupted".into()))?
            {
                self.observed
                    .fetch_add(chunk.len() as u64, std::sync::atomic::Ordering::Relaxed);
                if payload.len() as u64 + chunk.len() as u64 > partition.byte_size {
                    return Err(ReaderError::Integrity(
                        "object exceeds pinned byte size".into(),
                    ));
                }
                payload.extend_from_slice(&chunk);
            }
            Ok(payload.freeze())
        })
    }
}
