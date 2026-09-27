//! Provider boundaries for the equity, option, and crypto ingest transactions.
//!
//! The Python transactions accept a duck-typed client with `get_trades` /
//! `get_quotes` / `get_option_trades` / `get_option_quotes` coroutines. The
//! Rust port names those boundaries as object-safe traits so the locally
//! constructed [`AlpacaClient`] and test fakes are interchangeable, and the
//! transaction owns one boxed future per requested data type.

use futures_util::future::BoxFuture;
use lake_catalog::ContractRecord;
use lake_contracts::DateTime;
use lake_providers::{
    AlpacaClient, NormalizedCryptoQuote, NormalizedCryptoTrade, NormalizedOptionQuote,
    NormalizedOptionTrade, NormalizedQuote, NormalizedTrade, ProviderError,
};

use crate::options::transaction::OptionMarketDataClient;

/// Provider boundary for the two equity one-symbol fetches.
pub trait EquityDataProvider: Send + Sync {
    /// Fetch normalized trades for one symbol and inclusive session range.
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>>;

    /// Fetch normalized quotes for one symbol and inclusive session range.
    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>>;
}

impl EquityDataProvider for AlpacaClient {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedTrade>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client.fetch_trades(symbols, start, end).await
        })
    }

    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client.fetch_quotes(symbols, start, end).await
        })
    }
}

/// Provider boundary for the two crypto pair-day fetches.
pub trait CryptoDataProvider: Send + Sync {
    /// Fetch normalized crypto trades for one pair and inclusive day range.
    fn fetch_crypto_trades<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoTrade>, ProviderError>>;

    /// Fetch normalized crypto quotes for one pair and inclusive day range.
    fn fetch_crypto_quotes<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoQuote>, ProviderError>>;
}

impl CryptoDataProvider for AlpacaClient {
    fn fetch_crypto_trades<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoTrade>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client.fetch_crypto_trades(pairs, start, end).await
        })
    }

    fn fetch_crypto_quotes<'a>(
        &'a self,
        pairs: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedCryptoQuote>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client.fetch_crypto_quotes(pairs, start, end).await
        })
    }
}

/// Provider boundary for the selected OPRA contract fetches.
impl OptionMarketDataClient for AlpacaClient {
    fn get_option_trades<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionTrade>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client.fetch_option_trades(contracts, start, end).await
        })
    }

    fn get_option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionQuote>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client.fetch_option_quotes(contracts, start, end).await
        })
    }
}
