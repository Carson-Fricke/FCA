//! Authenticated catalog coverage route.

use axum::extract::State;
use axum::Json;
use lake_contracts::AssetClass;
use lake_coverage::{requested_coverage, EnsureSpec};

use crate::error::ApiError;
use crate::models::{
    CatalogCoverageResponse, CoverageQuery, CoverageQueryRaw, CoverageResponseBody,
    OptionCatalogCoverageResponse, OptionCoverageEntryResponse,
};
use crate::selection::{contracts_or_default, option_coverage, OptionRequestLike, OptionSelection};
use crate::state::ApiContext;
use crate::validate::StrictQuery;

/// Return computed coverage statuses without starting ingest work.
pub(crate) async fn coverage(
    State(context): State<ApiContext>,
    StrictQuery(raw): StrictQuery<CoverageQueryRaw>,
) -> Result<Json<CoverageResponseBody>, ApiError> {
    let request = CoverageQuery::parse(raw)?;
    let as_of = context.coverage_as_of();
    let catalog = context.state.catalog.as_ref();

    if request.asset_class == AssetClass::Option {
        let contracts = contracts_or_default(context.state.contracts.as_deref());
        let like = OptionRequestLike {
            data_type: request.data_type,
            symbol: request.symbol.as_deref(),
            underlying: request.underlying.as_deref(),
            contract_symbols: request.contract_symbols.as_deref(),
            contracts: request.contracts.as_deref(),
            start_date: request.start_date,
            end_date: request.end_date,
        };
        let (report, selection) = option_coverage(
            catalog,
            &like,
            Some(contracts),
            context.state.front_sets.as_deref(),
            as_of,
        )
        .map_err(|error| ApiError::from_coverage(&error))?;
        return Ok(Json(CoverageResponseBody::Option(option_response(
            &report,
            request.data_type,
            &selection,
        ))));
    }

    let spec = EnsureSpec {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: request.symbol.clone().unwrap_or_default(),
        start_date: request.start_date,
        end_date: request.end_date,
        interval: request.interval.clone(),
    };
    let report = requested_coverage(
        catalog,
        context.state.listings.as_deref(),
        &spec,
        as_of,
        "catalog",
    )
    .map_err(|error| ApiError::from_coverage(&error))?;
    Ok(Json(CoverageResponseBody::Standard(
        CatalogCoverageResponse::from_report(&report, request.data_type, &spec.symbol),
    )))
}

/// Render the shared coverage states with the OSI symbol on every entry.
pub(crate) fn option_response(
    report: &lake_coverage::CoverageReport,
    data_type: lake_contracts::DataType,
    selection: &OptionSelection,
) -> OptionCatalogCoverageResponse {
    OptionCatalogCoverageResponse {
        asset_class: AssetClass::Option.value().to_string(),
        data_type: data_type.value().to_string(),
        underlying: selection.underlying.clone(),
        contracts: selection.symbols(),
        start_date: report.start_date,
        end_date: report.end_date,
        entries: report
            .entries
            .iter()
            .map(OptionCoverageEntryResponse::from_session)
            .collect(),
    }
}
