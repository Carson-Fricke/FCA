//! Point-in-time contract resolution, including quote-implied model delta.

use futures_util::future::BoxFuture;
use lake_catalog::{ContractRecord, ContractRepository, OptionType};
use lake_contracts::{DataError, Date, DateTime, OptionRight, OptionSelector, ResolvedOption};
use lake_providers::{AlpacaClient, NormalizedOptionQuote, NormalizedQuote, ProviderError};
use std::sync::Arc;

/// External market inputs needed to select a contract without future Greeks.
pub trait OptionSelectionProvider: Send + Sync {
    fn contracts<'a>(
        &'a self,
        underlying: &'a str,
        start: Date,
        end: Date,
    ) -> BoxFuture<'a, Result<Vec<ContractRecord>, ProviderError>>;
    fn option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionQuote>, ProviderError>>;
    fn underlying_quotes<'a>(
        &'a self,
        underlying: &'a str,
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>>;
}

impl OptionSelectionProvider for AlpacaClient {
    fn contracts<'a>(
        &'a self,
        underlying: &'a str,
        start: Date,
        end: Date,
    ) -> BoxFuture<'a, Result<Vec<ContractRecord>, ProviderError>> {
        Box::pin(self.fetch_option_contracts("https://api.alpaca.markets", underlying, start, end))
    }
    fn option_quotes<'a>(
        &'a self,
        contracts: &'a [ContractRecord],
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedOptionQuote>, ProviderError>> {
        Box::pin(self.fetch_option_quotes(contracts, start, end))
    }
    fn underlying_quotes<'a>(
        &'a self,
        underlying: &'a str,
        start: DateTime,
        end: DateTime,
    ) -> BoxFuture<'a, Result<Vec<NormalizedQuote>, ProviderError>> {
        Box::pin(async move {
            self.fetch_quotes(&[underlying.to_string()], start, end)
                .await
        })
    }
}

pub struct OptionContractResolver {
    provider: Arc<dyn OptionSelectionProvider>,
    contracts: Arc<dyn ContractRepository>,
}

impl OptionContractResolver {
    pub fn new(
        provider: Arc<dyn OptionSelectionProvider>,
        contracts: Arc<dyn ContractRepository>,
    ) -> Self {
        Self {
            provider,
            contracts,
        }
    }

    pub async fn resolve(&self, request: &OptionSelector) -> Result<ResolvedOption, DataError> {
        let as_of = request.validate()?;
        let target = request
            .expiration
            .as_deref()
            .and_then(Date::parse)
            .unwrap_or_else(|| {
                as_of
                    .date()
                    .add_days(request.days_to_expiration.unwrap_or(0) as i32)
            });
        let tolerance = if request.expiration.is_some() {
            0
        } else {
            request.dte_tolerance as i32
        };
        let start = target.add_days(-tolerance).max(as_of.date().add_days(1));
        let end = target.add_days(tolerance);
        let right = match request.right {
            OptionRight::Call => OptionType::Call,
            OptionRight::Put => OptionType::Put,
        };
        let strike = request
            .strike
            .as_deref()
            .map(str::parse::<lake_contracts::Decimal18>)
            .transpose()
            .map_err(|e| DataError::value(e.to_string()))?;
        let mut contracts: Vec<_> = self
            .provider
            .contracts(&request.underlying, start, end)
            .await
            .map_err(provider_error)?
            .into_iter()
            .filter(|c| {
                c.underlying == request.underlying && c.option_type == right
                // Adjusted deliverables need a richer pricing contract.
                && c.multiplier == 100 && c.symbol.len() > 15
                && c.symbol[..c.symbol.len()-15] == request.underlying
                && c.expiration_date >= start && c.expiration_date <= end
                && strike.is_none_or(|s| c.strike_price == s)
                && c.listed_date.is_none_or(|d| d <= as_of.date())
                && c.delisted_date.is_none_or(|d| d >= as_of.date())
            })
            .collect();
        if contracts.is_empty() {
            return Err(DataError::NotFound("no eligible option contracts".into()));
        }
        // A historical quote proves the contract existed at selection time;
        // today's contract master alone cannot establish historical membership.
        let quote_start = DateTime::from_micros(
            as_of.micros() - i64::from(request.max_quote_age_seconds) * 1_000_000,
        );
        let quotes = self
            .provider
            .option_quotes(&contracts, quote_start, as_of)
            .await
            .map_err(provider_error)?;
        let underlying = if request.delta.is_some() {
            self.provider
                .underlying_quotes(&request.underlying, quote_start, as_of)
                .await
                .map_err(provider_error)?
        } else {
            Vec::new()
        };
        let mut candidates = Vec::new();
        for contract in contracts.drain(..) {
            let Some(quote) = quotes
                .iter()
                .filter(|q| {
                    q.symbol == contract.symbol
                        && q.timestamp >= quote_start
                        && q.timestamp <= as_of
                        && q.bid_price.is_positive()
                        && q.ask_price >= q.bid_price
                        && q.bid_size > 0
                        && q.ask_size > 0
                })
                .max_by_key(|q| q.timestamp)
            else {
                continue;
            };
            let delta = if request.delta.is_some() {
                let Some(spot) = underlying
                    .iter()
                    .filter(|q| {
                        q.symbol == request.underlying
                            && q.timestamp >= quote_start
                            && q.timestamp <= quote.timestamp
                            && q.bid_price.is_positive()
                            && q.ask_price >= q.bid_price
                            && q.bid_size > 0
                            && q.ask_size > 0
                    })
                    .max_by_key(|q| q.timestamp)
                else {
                    continue;
                };
                let expiry_time = lake_contracts::xnys_sessions(
                    contract.expiration_date.add_days(-7),
                    contract.expiration_date,
                )?
                .last()
                .map(|s| s.close_time)
                .ok_or_else(|| DataError::value("no expiration session"))?;
                let years = (expiry_time.micros() - quote.timestamp.micros()) as f64
                    / (365.0 * 86400.0 * 1e6);
                let Some(delta) = implied_delta(
                    mid(quote.bid_price, quote.ask_price),
                    mid(spot.bid_price, spot.ask_price),
                    number(contract.strike_price),
                    years,
                    request.risk_free_rate.parse().unwrap(),
                    request.dividend_yield.parse().unwrap(),
                    request.right,
                ) else {
                    continue;
                };
                Some(delta)
            } else {
                None
            };
            let delta_distance = delta
                .map(|d| {
                    (d.abs() - request.delta.as_deref().unwrap().parse::<f64>().unwrap()).abs()
                })
                .unwrap_or(0.0);
            if delta_distance > request.delta_tolerance.parse::<f64>().unwrap() {
                continue;
            }
            candidates.push((contract, quote.timestamp, delta, delta_distance));
        }
        candidates.sort_by(|a, b| {
            a.0.expiration_date
                .days_until(target)
                .abs()
                .cmp(&b.0.expiration_date.days_until(target).abs())
                .then_with(|| a.3.total_cmp(&b.3))
                .then_with(|| a.0.expiration_date.cmp(&b.0.expiration_date))
                .then_with(|| a.0.strike_price.cmp(&b.0.strike_price))
                .then_with(|| a.0.symbol.cmp(&b.0.symbol))
        });
        let (contract, quote_time, delta, _) = candidates.into_iter().next().ok_or_else(|| {
            DataError::NotFound(
                "no option meets selector with usable point-in-time quotes and delta tolerance"
                    .into(),
            )
        })?;
        self.contracts.upsert(&contract)?;
        Ok(ResolvedOption {
            symbol: contract.symbol,
            underlying: contract.underlying,
            expiration: contract.expiration_date.isoformat(),
            strike: contract.strike_price.to_string(),
            right: request.right,
            multiplier: contract.multiplier,
            as_of: request.as_of.clone(),
            delta: delta.map(|d| format!("{d:.12}")),
            quote_time: Some(quote_time.isoformat_seconds_z()),
            method: if delta.is_some() {
                "quote_implied_black_scholes_v1"
            } else {
                "contract_and_historical_quote_v1"
            }
            .into(),
        })
    }
}

fn provider_error(error: ProviderError) -> DataError {
    DataError::Io(error.to_string())
}
fn number(value: lake_contracts::Decimal18) -> f64 {
    value.to_string().parse().expect("finite decimal")
}
fn mid(a: lake_contracts::Decimal18, b: lake_contracts::Decimal18) -> f64 {
    (number(a) + number(b)) / 2.0
}

fn normal_cdf(x: f64) -> f64 {
    let t = 1.0 / (1.0 + 0.2316419 * x.abs());
    let polynomial = t
        * (0.319381530
            + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
    let tail = (-x * x / 2.0).exp() / (2.0 * std::f64::consts::PI).sqrt() * polynomial;
    if x >= 0.0 {
        1.0 - tail
    } else {
        tail
    }
}

/// European-model estimate for US option selection; not a provider Greek.
/// Rates/yield are explicit selector inputs; unpriceable quotes are excluded.
pub fn implied_delta(
    price: f64,
    spot: f64,
    strike: f64,
    years: f64,
    rate: f64,
    yield_: f64,
    right: OptionRight,
) -> Option<f64> {
    if [price, spot, strike, years, rate, yield_]
        .iter()
        .any(|x| !x.is_finite())
        || price <= 0.0
        || spot <= 0.0
        || strike <= 0.0
        || years <= 0.0
    {
        return None;
    }
    let discounted_spot = spot * (-yield_ * years).exp();
    let discounted_strike = strike * (-rate * years).exp();
    let (lower, upper) = match right {
        OptionRight::Call => (
            (discounted_spot - discounted_strike).max(0.0),
            discounted_spot,
        ),
        OptionRight::Put => (
            (discounted_strike - discounted_spot).max(0.0),
            discounted_strike,
        ),
    };
    if price <= lower || price >= upper {
        return None;
    }
    let model = |vol: f64| {
        let width = vol * years.sqrt();
        let d1 = ((spot / strike).ln() + (rate - yield_ + vol * vol / 2.0) * years) / width;
        let d2 = d1 - width;
        match right {
            OptionRight::Call => {
                discounted_spot * normal_cdf(d1) - discounted_strike * normal_cdf(d2)
            }
            OptionRight::Put => {
                discounted_strike * normal_cdf(-d2) - discounted_spot * normal_cdf(-d1)
            }
        }
    };
    let (mut lo, mut hi) = (0.000001, 10.0);
    if model(hi) < price {
        return None;
    }
    for _ in 0..100 {
        let v = (lo + hi) / 2.0;
        if model(v) < price {
            lo = v;
        } else {
            hi = v;
        }
    }
    let vol = (lo + hi) / 2.0;
    let d1 =
        ((spot / strike).ln() + (rate - yield_ + vol * vol / 2.0) * years) / (vol * years.sqrt());
    Some(
        (-yield_ * years).exp()
            * match right {
                OptionRight::Call => normal_cdf(d1),
                OptionRight::Put => normal_cdf(d1) - 1.0,
            },
    )
}
