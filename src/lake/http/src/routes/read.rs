//! Authenticated streaming Arrow IPC read route.
//!
//! `GET /v1/read` plans the ensured terminal partitions for one symbol window
//! (the file query's classification rules), then streams the frozen dataset
//! schema and every stored record batch through the injected blob-read seam as
//! one `application/vnd.apache.arrow.stream` body. Objects are decoded one at
//! a time and batches are forwarded as they are encoded, so memory stays
//! bounded and the client can consume rows before the last session is read.

use std::io;

use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::Response;
use lake_contracts::{AssetClass, DateTime};
use lake_query::{plan_from_entries, plan_read, FileQueryContext, ReadPlan, ReadRequest};
use lake_storage::write_dataset_ipc_stream;

use crate::error::ApiError;
use crate::models::{ReadQuery, ReadQueryRaw};
use crate::routes::query::classify_option;
use crate::selection::{contracts_or_default, option_coverage, OptionRequestLike};
use crate::state::ApiContext;
use crate::validate::StrictQuery;

/// Stream the ensured Arrow IPC stream for the requested window.
pub(crate) async fn read(
    State(context): State<ApiContext>,
    StrictQuery(raw): StrictQuery<ReadQueryRaw>,
) -> Result<Response, ApiError> {
    let query = ReadQuery::parse(raw)?;
    let as_of = context.coverage_as_of();
    let plan = plan(&context, &query, as_of)?;
    if plan.files.is_empty() {
        return Err(no_data());
    }
    let store = context.read_store()?;
    let object_paths: Vec<String> = plan
        .files
        .iter()
        .map(|file| file.object_path.clone())
        .collect();
    let asset_class = plan.asset_class;
    let data_type = plan.data_type;

    let (sender, receiver) = tokio::sync::mpsc::channel::<Result<Bytes, io::Error>>(1);
    let error_sender = sender.clone();
    tokio::task::spawn_blocking(move || {
        let writer = ChannelWriter { sender };
        if let Err(error) =
            write_dataset_ipc_stream(&*store, &object_paths, asset_class, data_type, writer)
        {
            let _ = error_sender.blocking_send(Err(io::Error::other(error.to_string())));
        }
    });

    let stream = futures_util::stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|item| (item, receiver))
    });
    Response::builder()
        .status(StatusCode::OK)
        .header(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/vnd.apache.arrow.stream"),
        )
        .body(Body::from_stream(stream))
        .map_err(|error| ApiError::internal(error.to_string()))
}

/// Plan the ensured files for one validated read request.
fn plan(context: &ApiContext, query: &ReadQuery, as_of: DateTime) -> Result<ReadPlan, ApiError> {
    if query.asset_class == AssetClass::Option {
        return plan_option(context, query, as_of);
    }
    plan_read(
        &FileQueryContext {
            catalog: context.state.catalog.as_ref(),
            listings: context.state.listings.as_deref(),
            sas_issuer: None,
            ensure_callback: None,
            as_of,
            bounded_query: None,
            force_authorized: false,
        },
        &ReadRequest {
            asset_class: query.asset_class,
            data_type: query.data_type,
            symbol: query.symbol.clone(),
            interval: query.interval.clone(),
            start_date: query.start_date,
            end_date: query.end_date,
        },
    )
    .map_err(|error| ApiError::from_query(&error))
}

/// Classify one option read through the shared option-selection surface.
fn plan_option(
    context: &ApiContext,
    query: &ReadQuery,
    as_of: DateTime,
) -> Result<ReadPlan, ApiError> {
    let contracts = contracts_or_default(context.state.contracts.as_deref());
    let like = OptionRequestLike {
        data_type: query.data_type,
        symbol: Some(query.symbol.as_str()),
        underlying: None,
        contract_symbols: None,
        contracts: None,
        start_date: query.start_date,
        end_date: query.end_date,
    };
    let (report, _selection) = option_coverage(
        context.state.catalog.as_ref(),
        &like,
        Some(contracts),
        context.state.front_sets.as_deref(),
        as_of,
    )
    .map_err(|error| ApiError::from_coverage(&error))?;
    let (usable, _incomplete) = classify_option(&report, query.data_type);
    plan_from_entries(
        AssetClass::Option,
        query.data_type,
        query.symbol.clone(),
        query.interval.clone(),
        &usable,
    )
    .map_err(|error| ApiError::from_query(&error))
}

/// The `no_data` rejection when the window has no ensured terminal files.
fn no_data() -> ApiError {
    ApiError::new(
        StatusCode::NOT_FOUND,
        "no_data",
        "no ensured files exist for the requested window",
    )
}

/// A [`std::io::Write`] sink forwarding each encoded chunk to the response.
struct ChannelWriter {
    sender: tokio::sync::mpsc::Sender<Result<Bytes, io::Error>>,
}

impl std::io::Write for ChannelWriter {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.sender
            .blocking_send(Ok(Bytes::copy_from_slice(buffer)))
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "client disconnected"))?;
        Ok(buffer.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
