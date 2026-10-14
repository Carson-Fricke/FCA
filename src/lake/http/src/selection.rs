//! Option contract selection shared by the coverage, ensure, and query routes.
//!
//! Ports `lake.http.routes.catalog`'s `resolve_option_selection`,
//! `option_coverage`, and `_validate_contracts`: explicit OSI symbols or
//! persisted daily front-set membership resolve to one deterministic contract
//! set, and the same Python policy codes and messages are raised when metadata
//! is missing, a contract is unknown, or the requested underlying disagrees.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use lake_catalog::{
    CatalogRepository, ContractRecord, ContractRepository, CoverageStatus, DailyFrontSetRepository,
    MemoryContractRepository,
};
use lake_contracts::{AssetClass, DataType, Date, DateTime};
use lake_coverage::{
    sessions_for, CoverageReport, CoverageRequestError, CoverageSession, CoverageState,
    SessionCoverage,
};

/// The selection fields shared by every option request shape.
#[derive(Debug, Clone, Copy)]
pub(crate) struct OptionRequestLike<'a> {
    /// Requested data type.
    pub data_type: DataType,
    pub interval: Option<&'a str>,
    /// Legacy single-symbol selector.
    pub symbol: Option<&'a str>,
    /// Requested underlying.
    pub underlying: Option<&'a str>,
    /// Explicitly selected OSI contracts.
    pub contract_symbols: Option<&'a [String]>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
}

/// Canonical option membership resolved for one request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct OptionSelection {
    /// Canonical uppercase underlying, when resolved.
    pub underlying: Option<String>,
    /// Selected contracts per session date.
    pub symbols_by_date: BTreeMap<Date, Vec<String>>,
}

impl OptionSelection {
    /// Selected OSI symbols across every session, sorted and deduplicated.
    #[must_use]
    pub fn symbols(&self) -> Vec<String> {
        self.symbols_by_date
            .values()
            .flatten()
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

/// Calculate option coverage against XNYS sessions and OSI partitions.
pub(crate) fn option_coverage(
    catalog: &dyn CatalogRepository,
    request: &OptionRequestLike<'_>,
    contract_repository: Option<&dyn ContractRepository>,
    front_set_repository: Option<&dyn DailyFrontSetRepository>,
    as_of: DateTime,
) -> Result<(CoverageReport, OptionSelection), CoverageRequestError> {
    let sessions = sessions_for(
        AssetClass::Equity,
        request.start_date,
        request.end_date,
        as_of,
    )?;
    let selection = resolve_option_selection(
        request,
        sessions.iter().map(|session| session.session_date),
        contract_repository,
        front_set_repository,
    )?;
    let mut entries = Vec::new();
    for session in &sessions {
        let selected = selection
            .symbols_by_date
            .get(&session.session_date)
            .map_or(&[][..], Vec::as_slice);
        for symbol in selected {
            entries.push(option_entry(
                catalog,
                request.data_type,
                request.interval,
                symbol,
                session,
            )?);
        }
    }
    Ok((
        CoverageReport {
            asset_class: AssetClass::Option,
            start_date: request.start_date,
            end_date: request.end_date,
            entries,
        },
        selection,
    ))
}

/// Resolve explicit OSI symbols or persisted front-set membership.
pub(crate) fn resolve_option_selection(
    request: &OptionRequestLike<'_>,
    session_dates: impl IntoIterator<Item = Date>,
    contract_repository: Option<&dyn ContractRepository>,
    front_set_repository: Option<&dyn DailyFrontSetRepository>,
) -> Result<OptionSelection, CoverageRequestError> {
    let dates: Vec<Date> = session_dates.into_iter().collect();
    let mut underlying = canonical_optional(request.underlying)?;
    let symbol = canonical_optional(request.symbol)?;
    let requested = request.contract_symbols;
    let mut symbols: Vec<String> = match requested {
        Some(values) => values
            .iter()
            .map(|value| canonical(value))
            .collect::<Result<_, _>>()?,
        None => Vec::new(),
    };

    if symbols.is_empty() && symbol.is_some() {
        let symbol = symbol.as_deref().expect("checked above");
        let contract = match contract_repository {
            Some(repository) => repository.get(symbol).map_err(CoverageRequestError::from)?,
            None => None,
        };
        if let Some(contract) = contract.as_ref().filter(|contract| {
            underlying.is_none() || Some(contract.underlying.as_str()) == underlying.as_deref()
        }) {
            symbols = vec![contract.symbol.clone()];
            underlying = Some(contract.underlying.clone());
        } else if underlying.is_none() {
            if let Some(repository) = contract_repository {
                let candidates = repository
                    .list(Some(symbol), None)
                    .map_err(CoverageRequestError::from)?;
                if candidates.is_empty() {
                    select_symbol_without_metadata(
                        symbol,
                        &mut symbols,
                        &mut underlying,
                        front_set_repository,
                    )?;
                } else {
                    underlying = Some(symbol.to_string());
                }
            } else {
                select_symbol_without_metadata(
                    symbol,
                    &mut symbols,
                    &mut underlying,
                    front_set_repository,
                )?;
            }
        } else {
            symbols = vec![symbol.to_string()];
        }
    }

    if !symbols.is_empty() {
        let records = validate_contracts(&symbols, underlying.as_deref(), contract_repository)?;
        if underlying.is_none() && !records.is_empty() {
            let known: BTreeSet<&str> = records
                .iter()
                .map(|record| record.underlying.as_str())
                .collect();
            if known.len() == 1 {
                underlying = Some(
                    known
                        .into_iter()
                        .next()
                        .expect("single known underlying")
                        .to_string(),
                );
            }
        }
        let selected: Vec<String> = symbols
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let symbols_by_date = dates.iter().map(|date| (*date, selected.clone())).collect();
        return Ok(OptionSelection {
            underlying,
            symbols_by_date,
        });
    }

    if underlying.is_none() {
        return Err(CoverageRequestError::new(
            "option_selection_unavailable",
            "an option underlying or selected contract is required",
            None,
        ));
    }
    if contract_repository.is_none() && front_set_repository.is_none() {
        return Err(CoverageRequestError::new(
            "option_selection_unavailable",
            "option contract metadata or a persisted front set is required",
            None,
        ));
    }

    let selected_underlying = underlying.as_deref().expect("checked above");
    let mut selected_by_date: BTreeMap<Date, Vec<String>> = BTreeMap::new();
    for session_date in &dates {
        let front_set = match front_set_repository {
            Some(repository) => repository
                .get(selected_underlying, *session_date)
                .map_err(CoverageRequestError::from)?,
            None => None,
        };
        let fallback: Vec<String> = match contract_repository {
            Some(repository) => repository
                .list(Some(selected_underlying), Some(*session_date))
                .map_err(CoverageRequestError::from)?
                .into_iter()
                .map(|contract| contract.symbol)
                .collect(),
            None => Vec::new(),
        };
        let selected: Vec<String> = front_set
            .map_or(fallback, |front_set| front_set.symbols())
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        selected_by_date.insert(*session_date, selected);
    }
    if !dates.is_empty() && !selected_by_date.values().any(|symbols| !symbols.is_empty()) {
        return Err(CoverageRequestError::new(
            "option_selection_unavailable",
            "option contract metadata or a persisted front set is required",
            None,
        ));
    }
    Ok(OptionSelection {
        underlying,
        symbols_by_date: selected_by_date,
    })
}

/// Resolve the current symbol selector when no contract metadata resolves it.
///
/// Mirrors the Python tail that keeps an OSI-shaped symbol as an explicit
/// contract, treats any other shape as an underlying, and refuses a
/// non-OSI symbol when neither metadata nor a persisted front set can help.
fn select_symbol_without_metadata(
    symbol: &str,
    symbols: &mut Vec<String>,
    underlying: &mut Option<String>,
    front_set_repository: Option<&dyn DailyFrontSetRepository>,
) -> Result<(), CoverageRequestError> {
    if underlying.is_none() && front_set_repository.is_none() && !looks_like_option_symbol(symbol) {
        return Err(CoverageRequestError::new(
            "unsupported_asset_class",
            "option symbol must identify an OSI contract",
            None,
        ));
    }
    if underlying.is_none() {
        if looks_like_option_symbol(symbol) {
            *symbols = vec![symbol.to_string()];
        } else {
            *underlying = Some(symbol.to_string());
        }
    } else {
        *symbols = vec![symbol.to_string()];
    }
    Ok(())
}

/// Classify one selected contract session the way the option route does.
fn option_entry(
    catalog: &dyn CatalogRepository,
    data_type: DataType,
    interval: Option<&str>,
    symbol: &str,
    session: &CoverageSession,
) -> Result<SessionCoverage, CoverageRequestError> {
    let session_date = session.session_date;
    let record = catalog
        .get(
            AssetClass::Option,
            data_type,
            interval,
            symbol,
            session_date,
        )
        .map_err(CoverageRequestError::from)?;
    let state = match &record {
        Some(record) if record.status == CoverageStatus::Empty => CoverageState::Empty,
        _ if session.is_partial => CoverageState::Partial,
        None => CoverageState::Missing,
        Some(record) => {
            CoverageState::from_value(record.status.value()).unwrap_or(CoverageState::Missing)
        }
    };
    Ok(SessionCoverage {
        asset_class: AssetClass::Option,
        data_type,
        symbol: symbol.to_string(),
        session: *session,
        state,
        record,
    })
}

/// Validate explicit contracts against the repository and requested underlying.
fn validate_contracts(
    symbols: &[String],
    underlying: Option<&str>,
    repository: Option<&dyn ContractRepository>,
) -> Result<Vec<ContractRecord>, CoverageRequestError> {
    let Some(repository) = repository else {
        return Ok(Vec::new());
    };
    let mut records = Vec::with_capacity(symbols.len());
    for symbol in symbols {
        let record = repository.get(symbol).map_err(CoverageRequestError::from)?;
        let Some(record) = record else {
            return Err(CoverageRequestError::new(
                "option_contract_not_found",
                format!("selected option contract does not exist: {symbol}"),
                None,
            ));
        };
        records.push(record);
    }
    if let Some(underlying) = underlying {
        let known: BTreeSet<&str> = records
            .iter()
            .map(|record| record.underlying.as_str())
            .collect();
        if known != BTreeSet::from([underlying]) {
            return Err(CoverageRequestError::new(
                "option_underlying_mismatch",
                "selected option contracts must share the requested underlying",
                None,
            ));
        }
    }
    Ok(records)
}

/// Canonicalise a request symbol, rejecting blank values.
fn canonical(value: &str) -> Result<String, CoverageRequestError> {
    if value.trim().is_empty() {
        return Err(CoverageRequestError::new(
            "validation_error",
            "option symbols must be non-empty",
            None,
        ));
    }
    Ok(value.trim().to_uppercase())
}

/// Canonicalise an optional request symbol.
fn canonical_optional(value: Option<&str>) -> Result<Option<String>, CoverageRequestError> {
    value.map(canonical).transpose()
}

/// Recognize an OSI contract in the current single-symbol selector.
fn looks_like_option_symbol(symbol: &str) -> bool {
    let characters: Vec<char> = symbol.chars().collect();
    characters.len() >= 18 && matches!(characters[characters.len() - 9], 'C' | 'P')
}

/// Resolve the contract repository the Python application always has.
///
/// `create_app` defaults to an empty in-memory contract repository when none is
/// injected, so an absent repository still reports unknown explicit contracts
/// rather than skipping contract validation.
pub(crate) fn contracts_or_default(
    contracts: Option<&dyn ContractRepository>,
) -> &dyn ContractRepository {
    static EMPTY: OnceLock<MemoryContractRepository> = OnceLock::new();
    match contracts {
        Some(repository) => repository,
        None => EMPTY.get_or_init(MemoryContractRepository::new),
    }
}
