//! Serde-based extractors and the request validators shared by every route.
//!
//! Axum rejections and hand-written field checks both render the FastAPI
//! validation envelope: code `validation_error`, message
//! `request validation failed`, and a `details` array.

use axum::extract::rejection::JsonRejection;
use axum::extract::{FromRequest, FromRequestParts, Request};
use axum::http::request::Parts;
use axum::Json;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

use crate::error::ApiError;

/// Query keys whose single value is still a one-element list, as FastAPI's
/// `tuple[Symbol, ...]` query parameters accept both spellings.
const LIST_QUERY_FIELDS: &[&str] = &["contract_symbols"];

/// A body extractor mapping every serde rejection into the validation envelope.
pub(crate) struct StrictJson(pub Value);

impl<S> FromRequest<S> for StrictJson
where
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        match Json::<Value>::from_request(request, state).await {
            Ok(Json(value)) => Ok(StrictJson(value)),
            Err(rejection) => Err(json_rejection(rejection)),
        }
    }
}

/// A query extractor mapping every serde rejection into the validation envelope.
///
/// The query string is collected into a JSON object first so a repeated key
/// becomes an array, which lets serde deserialize both `a=1` and `a=1&a=2` into
/// a list field exactly like FastAPI's repeated query parameters.
pub(crate) struct StrictQuery<T>(pub T);

impl<S, T> FromRequestParts<S> for StrictQuery<T>
where
    T: DeserializeOwned + Send,
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let value = query_value(parts.uri.query().unwrap_or_default())?;
        let parsed = serde_json::from_value(value)
            .map_err(|error| ApiError::validation_error("query", error.to_string()))?;
        Ok(StrictQuery(parsed))
    }
}

fn json_rejection(rejection: JsonRejection) -> ApiError {
    ApiError::validation_error("body", rejection.body_text())
}

fn query_value(query: &str) -> Result<Value, ApiError> {
    let mut fields: Map<String, Value> = Map::new();
    if query.is_empty() {
        return Ok(Value::Object(fields));
    }
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, raw) = pair.split_once('=').unwrap_or((pair, ""));
        let key = decode_component(key)?;
        let value = Value::String(decode_component(raw)?);
        let single_is_list = LIST_QUERY_FIELDS.contains(&key.as_str());
        match fields.get_mut(&key) {
            Some(Value::Array(values)) => values.push(value),
            Some(existing) => {
                let first = existing.take();
                *existing = Value::Array(vec![first, value]);
            }
            None => {
                fields.insert(
                    key,
                    if single_is_list {
                        Value::Array(vec![value])
                    } else {
                        value
                    },
                );
            }
        }
    }
    Ok(Value::Object(fields))
}

fn decode_component(text: &str) -> Result<String, ApiError> {
    percent_encoding::percent_decode_str(&text.replace('+', " "))
        .decode_utf8()
        .map(|decoded| decoded.into_owned())
        .map_err(|_| ApiError::validation_error("query", "invalid percent encoding"))
}

/// Borrow the request body as a JSON object.
pub(crate) fn require_object(value: &Value) -> Result<&Map<String, Value>, ApiError> {
    value
        .as_object()
        .ok_or_else(|| ApiError::validation_error("body", "request body must be a JSON object"))
}

/// Reject any field outside the strict model, mirroring `extra="forbid"`.
pub(crate) fn reject_unknown(
    object: &Map<String, Value>,
    allowed: &[&str],
) -> Result<(), ApiError> {
    for key in object.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(ApiError::validation_error(
                "body",
                format!("Extra inputs are not permitted: {key}"),
            ));
        }
    }
    Ok(())
}

/// Read a required string field.
pub(crate) fn required_string(object: &Map<String, Value>, name: &str) -> Result<String, ApiError> {
    match object.get(name) {
        None | Some(Value::Null) => Err(ApiError::validation_error(
            "body",
            format!("Field required: {name}"),
        )),
        Some(Value::String(value)) => Ok(value.clone()),
        Some(_) => Err(ApiError::validation_error(
            "body",
            format!("Input should be a valid string: {name}"),
        )),
    }
}

/// Read an optional string field, treating `null` as absent.
pub(crate) fn optional_string(
    object: &Map<String, Value>,
    name: &str,
) -> Result<Option<String>, ApiError> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(ApiError::validation_error(
            "body",
            format!("Input should be a valid string: {name}"),
        )),
    }
}

/// Read an optional list of strings, treating `null` as absent.
pub(crate) fn optional_string_list(
    object: &Map<String, Value>,
    name: &str,
) -> Result<Option<Vec<String>>, ApiError> {
    match object.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Array(values)) => {
            let mut items = Vec::with_capacity(values.len());
            for value in values {
                match value {
                    Value::String(text) => items.push(text.clone()),
                    _ => {
                        return Err(ApiError::validation_error(
                            "body",
                            format!("Input should be a valid string: {name}"),
                        ))
                    }
                }
            }
            Ok(Some(items))
        }
        Some(_) => Err(ApiError::validation_error(
            "body",
            format!("Input should be a valid list: {name}"),
        )),
    }
}

/// Read an optional boolean field with a default.
pub(crate) fn optional_bool(
    object: &Map<String, Value>,
    name: &str,
    default: bool,
) -> Result<bool, ApiError> {
    match object.get(name) {
        None => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(ApiError::validation_error(
            "body",
            format!("Input should be a valid boolean: {name}"),
        )),
    }
}

/// Validate the strict `Symbol` field shape.
pub(crate) fn validate_symbol_field(value: &str, field: &str) -> Result<String, ApiError> {
    let length = value.chars().count();
    let valid_first = value
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric());
    let valid_rest = value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || "._/-".contains(character));
    if length == 0 || length > 64 || !valid_first || !valid_rest {
        return Err(ApiError::validation_error(
            field,
            "String should match pattern '^[A-Za-z0-9][A-Za-z0-9._/-]*$'",
        ));
    }
    Ok(value.to_string())
}

/// Validate an optional `Symbol` field.
pub(crate) fn validate_optional_symbol(
    value: Option<String>,
    field: &str,
) -> Result<Option<String>, ApiError> {
    value
        .map(|value| validate_symbol_field(&value, field))
        .transpose()
}

/// Validate a bounded list of `Symbol` values (1 through 100 items).
pub(crate) fn validate_symbol_list(
    values: Vec<String>,
    field: &str,
) -> Result<Vec<String>, ApiError> {
    if values.is_empty() || values.len() > 100 {
        return Err(ApiError::validation_error(
            field,
            "List should have at least 1 item and at most 100 items",
        ));
    }
    values
        .into_iter()
        .map(|value| validate_symbol_field(&value, field))
        .collect()
}

/// Validate an optional bounded list of `Symbol` values.
pub(crate) fn validate_optional_symbol_list(
    values: Option<Vec<String>>,
    field: &str,
) -> Result<Option<Vec<String>>, ApiError> {
    values
        .map(|values| validate_symbol_list(values, field))
        .transpose()
}

/// Parse a persisted `AssetClass` value.
pub(crate) fn parse_asset_class(value: &str) -> Result<lake_contracts::AssetClass, ApiError> {
    lake_contracts::AssetClass::from_value(value).ok_or_else(|| {
        ApiError::validation_error(
            "asset_class",
            "Input should be 'equity', 'option', 'crypto' or 'reference'",
        )
    })
}

/// Parse a persisted `DataType` value.
pub(crate) fn parse_data_type(value: &str) -> Result<lake_contracts::DataType, ApiError> {
    lake_contracts::DataType::from_value(value).ok_or_else(|| {
        ApiError::validation_error(
            "data_type",
            "Input should be 'trade', 'quote', 'bars', 'classification' or 'fundamentals'",
        )
    })
}

/// Validate the `(data_type, interval)` pairing the way the strict models do.
///
/// Bars requires exactly one canonical token; every other data type must not
/// carry an interval. `None` input stays `None`.
pub(crate) fn parse_interval(
    data_type: lake_contracts::DataType,
    interval: Option<String>,
) -> Result<Option<String>, ApiError> {
    use lake_contracts::{BarInterval, DataType};
    match (data_type, interval) {
        (DataType::Bars, None) => Err(ApiError::validation_error(
            "interval",
            "Value error, interval is required for bars",
        )),
        (DataType::Bars, Some(interval)) => {
            BarInterval::parse(&interval).map_err(|error| {
                ApiError::validation_error("interval", format!("Value error, {error}"))
            })?;
            Ok(Some(interval))
        }
        (_, Some(_)) => Err(ApiError::validation_error(
            "interval",
            "Value error, interval is supported for bars only",
        )),
        (_, None) => Ok(None),
    }
}

/// Parse an ISO calendar date.
pub(crate) fn parse_date(value: &str, field: &str) -> Result<lake_contracts::Date, ApiError> {
    lake_contracts::Date::parse(value)
        .ok_or_else(|| ApiError::validation_error(field, "Input should be a valid date"))
}

/// Reject a reversed closed date range with the Python message.
pub(crate) fn validate_date_range(
    start_date: lake_contracts::Date,
    end_date: lake_contracts::Date,
) -> Result<(), ApiError> {
    if end_date < start_date {
        return Err(ApiError::validation_error(
            "end_date",
            "Value error, end_date must not be before start_date",
        ));
    }
    Ok(())
}
