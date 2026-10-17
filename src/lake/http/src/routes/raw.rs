//! Authenticated raw-object metadata route.

use axum::extract::State;
use axum::Json;
use lake_catalog::CoverageRecord;
use lake_contracts::{partition_path, AssetClass, DataType, Date};

use crate::error::ApiError;
use crate::models::{RawObjectQueryRaw, RawObjectResponse};
use crate::state::ApiContext;
use crate::validate::StrictQuery;

/// Issue read access only for an exact, already-cataloged object path.
pub(crate) async fn raw_object(
    State(context): State<ApiContext>,
    StrictQuery(raw): StrictQuery<RawObjectQueryRaw>,
) -> Result<Json<RawObjectResponse>, ApiError> {
    let object_path = raw.object_path;
    if object_path.is_empty() || object_path.chars().count() > 512 {
        return Err(ApiError::validation_error(
            "object_path",
            "String should have at least 1 character and at most 512 characters",
        ));
    }
    let requested = catalog_object(&object_path)?;

    let record = context
        .state
        .catalog
        .get(
            requested.asset_class,
            requested.data_type,
            requested.interval.as_deref(),
            &requested.symbol,
            requested.session_date,
        )
        .map_err(ApiError::from)?;
    let Some(record) = record.filter(|record| authorizes(record, &requested)) else {
        return Err(ApiError::new(
            axum::http::StatusCode::NOT_FOUND,
            "raw_object_not_found",
            "the requested object is not available in the catalog",
        ));
    };
    let Some(issuer) = context.state.sas_issuer.as_deref() else {
        return Err(ApiError::sas_unavailable(
            "raw object SAS issuance is not configured",
        ));
    };

    let url = issuer
        .issue(&requested.object_path)
        .map_err(|error| ApiError::internal(error.to_string()))?;
    Ok(Json(RawObjectResponse {
        asset_class: record.asset_class.value().to_string(),
        data_type: record.data_type.value().to_string(),
        symbol: record.symbol,
        session_date: record.session_date,
        status: record.status.value().to_string(),
        object_path: requested.object_path,
        url: url.clone(),
        sas_url: url,
        row_count: record.row_count,
        byte_size: record.byte_size,
        checksum: record.checksum,
    }))
}

/// A catalog key and canonical path recovered from one raw object path.
#[derive(Debug, Clone, PartialEq, Eq)]
struct CatalogObject {
    asset_class: AssetClass,
    data_type: DataType,
    interval: Option<String>,
    symbol: String,
    session_date: Date,
    object_path: String,
}

/// Parse and validate a canonical lake partition path.
fn catalog_object(object_path: &str) -> Result<CatalogObject, ApiError> {
    let invalid = || {
        ApiError::new(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_object_path",
            "object_path must be a canonical lake partition path",
        )
    };
    if object_path.contains('\\')
        || object_path.starts_with('/')
        || object_path
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(invalid());
    }
    let checksum = lake_contracts::object_checksum(object_path).map_err(|_| invalid())?;
    let (_, logical) = object_path.split_once('/').ok_or_else(invalid)?;
    let segments: Vec<&str> = logical.split('/').collect();
    if segments.len() != 4 && segments.len() != 5 {
        return Err(invalid());
    }
    let date_text = segments[0].strip_prefix("date=").ok_or_else(invalid)?;
    if !is_iso_date(date_text) {
        return Err(invalid());
    }
    let session_date = Date::parse(date_text).ok_or_else(invalid)?;
    let asset_class = AssetClass::from_value(
        segments[1]
            .strip_prefix("asset_class=")
            .filter(|value| !value.is_empty())
            .ok_or_else(invalid)?,
    )
    .ok_or_else(invalid)?;
    let data_type = DataType::from_value(
        segments[2]
            .strip_prefix("data_type=")
            .filter(|value| !value.is_empty())
            .ok_or_else(invalid)?,
    )
    .ok_or_else(invalid)?;
    let (interval, symbol_segment) = if data_type == DataType::Bars {
        if segments.len() != 5 {
            return Err(invalid());
        }
        let interval = segments[3]
            .strip_prefix("interval=")
            .filter(|value| !value.is_empty())
            .ok_or_else(invalid)?
            .to_string();
        (Some(interval), segments[4])
    } else {
        if segments.len() != 4 {
            return Err(invalid());
        }
        (None, segments[3])
    };
    let symbol_text = symbol_segment
        .strip_suffix(".parquet")
        .filter(|value| !value.is_empty())
        .ok_or_else(invalid)?;
    let symbol = percent_encoding::percent_decode_str(symbol_text)
        .decode_utf8()
        .map(|decoded| decoded.into_owned())
        .map_err(|_| invalid())?;
    if symbol.chars().count() > 64
        || symbol
            .replace('\\', "/")
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(invalid());
    }
    let canonical = partition_path(
        session_date,
        asset_class,
        data_type,
        interval.as_deref(),
        &symbol,
    )
    .and_then(|logical| lake_contracts::immutable_path(&logical, checksum))
    .map_err(|_| invalid())?;
    if object_path != canonical {
        return Err(invalid());
    }
    Ok(CatalogObject {
        asset_class,
        data_type,
        interval,
        symbol,
        session_date,
        object_path: canonical,
    })
}

/// Whether a path segment is exactly `YYYY-MM-DD` in ASCII digits.
fn is_iso_date(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| index == 4 || index == 7 || byte.is_ascii_digit())
}

/// Whether a catalog row authorizes exactly the requested object.
fn authorizes(record: &CoverageRecord, requested: &CatalogObject) -> bool {
    record.asset_class == requested.asset_class
        && record.data_type == requested.data_type
        && record.interval == requested.interval
        && record.symbol == requested.symbol
        && record.session_date == requested.session_date
        && record.object_path.as_deref() == Some(requested.object_path.as_str())
}
