//! Alpaca market-data client for the market lake.
//!
//! `lake-providers` ports `lake.providers.alpaca`: the async REST client with
//! cursor paging, retry/backoff including 429 `Retry-After`, a process-wide
//! request-budget governor, and the normalized equity and crypto records the
//! ingestion layer writes. HTTP transport is injected through
//! [`AlpacaTransport`], so tests exercise paging, retry, and slicing against a
//! fake transport while production uses [`ReqwestTransport`].

pub mod client;
pub mod config;
pub mod error;
pub mod governor;
pub mod lane;
mod parse;
pub mod records;
pub mod transport;

pub use client::{plan_time_slices, AlpacaClient};
pub use config::ProviderConfig;
pub use error::ProviderError;
pub use governor::RequestGovernor;
pub use lane::{lane_for, Lane, LanePermit, LaneTuning};
pub use records::{NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedQuote, NormalizedTrade};
pub use transport::{
    AlpacaTransport, BoxFuture, ReqwestTransport, TransportFailure, TransportResponse,
};
