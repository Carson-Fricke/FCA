//! Cross-process rolling request reservations; storage adapters serialize grants.
use lake_contracts::DataError;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const WINDOW_MICROS: i64 = 60_000_000;
const PERMIT_LIFETIME_MICROS: i64 = 1_000_000;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BudgetState {
    pub limit: u32,
    pub observed_at: i64,
    pub charges: Vec<BudgetCharge>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BudgetCharge {
    pub count: u32,
    pub release_after: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BudgetGrant {
    pub count: u32,
    /// Measured from before the reservation RPC, never from a worker wall clock.
    pub valid_for: std::time::Duration,
    pub retry_after: std::time::Duration,
}
impl BudgetState {
    pub fn new(limit: u32) -> Result<Self, DataError> {
        if !(1..=10_000).contains(&limit) {
            return Err(DataError::value(
                "provider request budget must be between 1 and 10000",
            ));
        }
        Ok(Self {
            limit,
            observed_at: 0,
            charges: Vec::new(),
        })
    }
    /// Keep every reservation charged until sixty seconds after its last possible use.
    /// Lost acknowledgements, unused permits and worker restarts cannot refund capacity.
    pub fn reserve(&mut self, limit: u32, count: u32, now: i64) -> Result<BudgetGrant, DataError> {
        self.reserve_with_precision(limit, count, now, 0)
    }
    /// Authority clocks may truncate observations. Retain capacity through that
    /// precision interval as well as the last possible permit use and rolling minute.
    pub fn reserve_with_precision(
        &mut self,
        limit: u32,
        count: u32,
        now: i64,
        precision_micros: i64,
    ) -> Result<BudgetGrant, DataError> {
        if self.limit != limit || count == 0 {
            return Err(DataError::value("request budget configuration mismatch"));
        }
        if !(0..=1_000_000).contains(&precision_micros) {
            return Err(DataError::value("invalid budget clock precision"));
        }
        if now < self.observed_at {
            return Ok(BudgetGrant {
                count: 0,
                valid_for: std::time::Duration::ZERO,
                retry_after: delay(self.observed_at, now),
            });
        }
        self.observed_at = now;
        self.charges.retain(|charge| charge.release_after > now);
        let used = self
            .charges
            .iter()
            .try_fold(0u32, |sum, charge| sum.checked_add(charge.count))
            .ok_or_else(|| DataError::value("invalid request budget charges"))?;
        let available = self
            .limit
            .checked_sub(used)
            .ok_or_else(|| DataError::value("request budget is oversubscribed"))?;
        let count = count.min(available);
        let valid_until = now.saturating_add(PERMIT_LIFETIME_MICROS);
        if count > 0 {
            self.charges.push(BudgetCharge {
                count,
                release_after: valid_until
                    .saturating_add(WINDOW_MICROS)
                    .saturating_add(precision_micros),
            });
        }
        Ok(BudgetGrant {
            count,
            valid_for: std::time::Duration::from_micros(PERMIT_LIFETIME_MICROS as u64),
            retry_after: delay(
                self.charges
                    .first()
                    .map_or(valid_until, |charge| charge.release_after),
                now,
            ),
        })
    }
    /// OS uptime resets on reboot. Outstanding reservations receive a full new
    /// hold instead of being refunded or waiting for the previous boot's uptime.
    pub fn hold_after_clock_restart(
        &mut self,
        now: i64,
        precision_micros: i64,
    ) -> Result<(), DataError> {
        if !(0..=1_000_000).contains(&precision_micros) {
            return Err(DataError::value("invalid budget clock precision"));
        }
        self.observed_at = now;
        self.charges = vec![BudgetCharge {
            count: self.limit,
            release_after: now
                .saturating_add(PERMIT_LIFETIME_MICROS)
                .saturating_add(WINDOW_MICROS)
                .saturating_add(precision_micros),
        }];
        Ok(())
    }
}
fn delay(until: i64, now: i64) -> std::time::Duration {
    std::time::Duration::from_micros(until.saturating_sub(now).max(0) as u64)
}
pub fn budget_scope(endpoint: &str, key: &str) -> String {
    hex::encode(Sha256::digest(format!("{endpoint}\0{key}").as_bytes()))
}
pub(crate) fn budget_path(scope: &str) -> Result<String, DataError> {
    if scope.len() != 64
        || !scope
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(DataError::value("invalid request budget scope"));
    }
    Ok(format!("ingestion/budgets/{scope}.json"))
}

impl crate::JobStore {
    pub async fn reserve_requests(
        &self,
        scope: &str,
        limit: u32,
        count: u32,
    ) -> Result<BudgetGrant, DataError> {
        let path = budget_path(scope)?;
        loop {
            let response = self
                .request(reqwest::Method::GET, &path)
                .await?
                .send()
                .await
                .map_err(crate::store::io)?;
            let authority_now = response
                .headers()
                .get(reqwest::header::DATE)
                .and_then(|value| value.to_str().ok())
                .ok_or_else(|| {
                    DataError::Protocol("request budget response has no authority Date".into())
                })?;
            let authority_now = httpdate::parse_http_date(authority_now)
                .map_err(crate::store::io)?
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(crate::store::io)?
                .as_micros()
                .min(i64::MAX as u128) as i64;
            let (mut state, etag) = if response.status() == reqwest::StatusCode::NOT_FOUND {
                (BudgetState::new(limit)?, None)
            } else {
                let response = crate::store::success(response).await?;
                let etag = response
                    .headers()
                    .get(reqwest::header::ETAG)
                    .and_then(|h| h.to_str().ok())
                    .ok_or_else(|| {
                        DataError::Protocol("request budget response has no ETag".into())
                    })?
                    .to_string();
                (
                    response
                        .json::<BudgetState>()
                        .await
                        .map_err(crate::store::io)?,
                    Some(etag),
                )
            };
            // HTTP Date truncates to a second; the holder uses a monotonic
            // lifetime anchored before this RPC, including all CAS retries.
            let grant = state.reserve_with_precision(limit, count, authority_now, 1_000_000)?;
            if grant.count == 0 {
                return Ok(grant);
            }
            let request = self
                .request(reqwest::Method::PUT, &path)
                .await?
                .header("x-ms-blob-type", "BlockBlob");
            let request = match etag {
                Some(etag) => request.header(reqwest::header::IF_MATCH, etag),
                None => request.header(reqwest::header::IF_NONE_MATCH, "*"),
            };
            let response = request
                .json(&state)
                .send()
                .await
                .map_err(crate::store::io)?;
            if matches!(response.status().as_u16(), 409 | 412) {
                tokio::task::yield_now().await;
                continue;
            }
            crate::store::success(response).await?;
            return Ok(grant);
        }
    }
}
