//! Provider boundaries for the equity, option, and crypto ingest transactions.
//!
//! The Python transactions accept a duck-typed client with `get_trades` /
//! `get_quotes` / `get_option_trades` / `get_option_quotes` coroutines. The
//! Rust port names those boundaries as object-safe traits so the locally
//! constructed [`AlpacaClient`] and test fakes are interchangeable, and the
//! transaction owns one boxed future per requested data type.

use futures_util::future::BoxFuture;
use lake_catalog::ContractRecord;
use lake_contracts::{AssetClass, BarInterval, DateTime};
use lake_providers::{
    AlpacaClient, NormalizedBar, NormalizedCryptoQuote, NormalizedCryptoTrade,
    NormalizedOptionQuote, NormalizedOptionTrade, NormalizedQuote, NormalizedTrade, ProviderError,
};

use crate::options::transaction::OptionMarketDataClient;

/// Provider boundary for the session-aligned bars fetch.
///
/// One call covers one symbol or pair and one inclusive window; the interval
/// selects the provider timeframe (`1Min` / `1Day`).
pub trait BarsDataProvider: Send + Sync {
    /// Fetch normalized bars for one symbol and inclusive window.
    fn fetch_bars<'a>(
        &'a self,
        asset_class: AssetClass,
        symbol: &'a str,
        interval: BarInterval,
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>>;
}

impl BarsDataProvider for AlpacaClient {
    fn fetch_bars<'a>(
        &'a self,
        asset_class: AssetClass,
        symbol: &'a str,
        interval: BarInterval,
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedBar>, ProviderError>> {
        Box::pin(async move {
            let client: &AlpacaClient = self;
            client
                .fetch_bars(asset_class, symbol, interval, start, end)
                .await
        })
    }
}

/// Bounded normalized acquisition pages.
pub trait EquityDataProvider: Send + Sync {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedTrade>;
    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedQuote>;
}
impl EquityDataProvider for AlpacaClient {
    fn fetch_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedTrade> {
        self.stream_trades(symbols, start, end)
    }
    fn fetch_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedQuote> {
        self.stream_quotes(symbols, start, end)
    }
}
/// Bounded normalized acquisition pages.
pub trait CryptoDataProvider: Send + Sync {
    fn fetch_crypto_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedCryptoTrade>;
    fn fetch_crypto_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedCryptoQuote>;
}
impl CryptoDataProvider for AlpacaClient {
    fn fetch_crypto_trades<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedCryptoTrade> {
        self.stream_crypto_trades(symbols, start, end)
    }
    fn fetch_crypto_quotes<'a>(
        &'a self,
        symbols: &'a [String],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedCryptoQuote> {
        self.stream_crypto_quotes(symbols, start, end)
    }
}
/// Provider boundary for the selected OPRA contract fetches.
impl OptionMarketDataClient for AlpacaClient {
    fn get_option_trades<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedOptionTrade> {
        self.stream_option_trades(contracts, start, end)
    }
    fn get_option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> lake_providers::RecordStream<'a, NormalizedOptionQuote> {
        self.stream_option_quotes(contracts, start, end)
    }
}
