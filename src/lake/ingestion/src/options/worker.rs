//! Queue controller handle for option ingestion.
//!
//! Ports `lake.ingestion.options.worker`. The shared queue worker itself is a
//! later workstream: this module provides the handler the future queue crate
//! composes — `OptionQueueHandler` decodes one provider-independent option
//! payload and delegates to the [`OptionIngestTransaction`], optionally
//! re-resolving the contract through the persisted front set so a message can
//! never publish work outside the cataloged membership.
//!
//! A fetch failure is not a handler failure: the transaction publishes error
//! coverage through the committer and the handler still succeeds, exactly as
//! the Python queue worker deletes such a delivery after an idempotent commit.

use std::sync::Arc;

use lake_catalog::{DailyFrontSetRepository, OptionType};
use lake_contracts::{DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::CoverageSession;
use serde_json::Value;

use crate::options::transaction::OptionIngestTransaction;

/// Decode one option queue payload and delegate to the transaction.
pub struct OptionQueueHandler {
    transaction: Arc<OptionIngestTransaction>,
    front_sets: Option<Arc<dyn DailyFrontSetRepository>>,
}

impl OptionQueueHandler {
    /// Build a handler over a transaction and optional front-set repository.
    pub fn new(
        transaction: Arc<OptionIngestTransaction>,
        front_sets: Option<Arc<dyn DailyFrontSetRepository>>,
    ) -> Self {
        Self {
            transaction,
            front_sets,
        }
    }

    /// Handle one queue payload.
    ///
    /// The payload must carry `asset_class: "option"`, a complete OSI contract
    /// identity, ISO session bounds with timezones, and a non-empty
    /// `data_types` array. When a front-set repository is injected, the
    /// contract must already be a member of that underlying's front set for
    /// the session date.
    pub async fn handle(&self, payload: &Value) -> Result<(), DataError> {
        if payload.get("asset_class").and_then(Value::as_str) != Some("option") {
            return Err(DataError::value(
                "option queue message must use asset_class 'option'",
            ));
        }
        let mut contract = contract_from_payload(payload)?;
        let session_date = required_date(payload, "session_date")?;
        if let Some(front_sets) = &self.front_sets {
            let front_set = front_sets.get(&contract.underlying, session_date)?;
            let Some(front_set) = front_set else {
                return Err(DataError::value("option front set is not cataloged"));
            };
            let Some(selected) = front_set
                .contracts
                .iter()
                .find(|item| item.symbol == contract.symbol)
            else {
                return Err(DataError::value(
                    "option symbol is not in the cataloged front set",
                ));
            };
            contract = selected.clone();
        }

        let session = CoverageSession::new(
            session_date,
            required_datetime(payload, "start_time")?,
            required_datetime(payload, "end_time")?,
            false,
        )
        .map_err(|error| DataError::value(error.to_string()))?;
        let data_types = payload_data_types(payload)?;
        self.transaction
            .run(contract, &session, Some(&data_types))
            .await?;
        Ok(())
    }
}

/// Build a contract record from the queue payload fields.
fn contract_from_payload(payload: &Value) -> Result<lake_catalog::ContractRecord, DataError> {
    let symbol = required_text(payload, "symbol")?;
    let underlying = required_text(payload, "underlying")?;
    let expiration = required_date(payload, "expiration_date")?;
    let strike_text = required_text(payload, "strike_price")?;
    let strike = Decimal18::parse(&strike_text)
        .map_err(|_| DataError::value("strike_price must be a decimal number"))?;
    let option_type = required_text(payload, "option_type")?;
    let option_type = OptionType::from_value(&option_type)
        .ok_or_else(|| DataError::value("option_type must be 'call' or 'put'"))?;
    lake_catalog::ContractRecord::new(
        symbol,
        underlying,
        expiration,
        strike,
        option_type,
        100,
        None,
        None,
    )
}

/// Parse the payload's `data_types` array.
fn payload_data_types(payload: &Value) -> Result<Vec<DataType>, DataError> {
    let raw = payload.get("data_types").ok_or_else(|| {
        DataError::value("option queue message data_types must be a non-empty array")
    })?;
    let Value::Array(values) = raw else {
        return Err(DataError::value(
            "option queue message data_types must be a non-empty array",
        ));
    };
    if values.is_empty() {
        return Err(DataError::value(
            "option queue message data_types must be a non-empty array",
        ));
    }
    values
        .iter()
        .map(|value| match value.as_str() {
            Some(text) => DataType::from_value(text)
                .ok_or_else(|| DataError::value(format!("{text:?} is not a valid DataType"))),
            None => Err(DataError::value(
                "option queue message data_types must contain strings",
            )),
        })
        .collect()
}

/// Return one required non-empty string field.
fn required_text(payload: &Value, name: &str) -> Result<String, DataError> {
    match payload.get(name) {
        Some(Value::String(value)) if !value.trim().is_empty() => Ok(value.clone()),
        _ => Err(DataError::value(format!(
            "option queue field '{name}' must be a non-empty string"
        ))),
    }
}

/// Return one required ISO date field.
fn required_date(payload: &Value, name: &str) -> Result<Date, DataError> {
    let value = required_text(payload, name)?;
    Date::parse(&value)
        .ok_or_else(|| DataError::value(format!("option queue field '{name}' must be an ISO date")))
}

/// Return one required timezone-aware ISO datetime field, normalized to UTC.
fn required_datetime(payload: &Value, name: &str) -> Result<DateTime, DataError> {
    let value = required_text(payload, name)?;
    let has_timezone =
        value.ends_with('Z') || value.rfind(['+', '-']).is_some_and(|index| index > 10);
    if !has_timezone {
        return Err(DataError::value(format!(
            "option queue field '{name}' must include a timezone"
        )));
    }
    DateTime::parse_iso_utc(&value).ok_or_else(|| {
        DataError::value(format!(
            "option queue field '{name}' must be an ISO datetime"
        ))
    })
}
