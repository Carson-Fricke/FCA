//! One-shot screening execution over the JIT seam.
//!
//! The host builds exactly one [`ScreenContext`] (universe, bars, facts, and
//! precomputed indicators) and runs the authored
//! [`ScreeningProgram`](harness_strategy_sdk::ScreeningProgram) once per
//! `as_of`; there is no order surface, no path or data-root parameter, and no
//! lake access in this crate. Ordering of the returned rows is part of the
//! contract and is applied host-side by [`rank_rows`].

use std::cmp::Ordering;

use harness_strategy_sdk::ScreenContext;
use harness_strategy_sdk::ScreenRow;
use num::Decimal;

use harness_strategy_jit::compile;
use harness_strategy_jit::JitError;
use harness_strategy_jit::LoadedScreening;
use harness_strategy_jit::ManagedWorkspace;
use harness_strategy_jit::ProgramKind;

/// Compile, load, and run one authored screening program.
///
/// The result rows are ordered host-side by [`rank_rows`]: `rank_metric`
/// descending with exact decimal comparison, then symbol ascending; rows
/// missing the metric sort last, symbol ascending.
///
/// # Errors
///
/// Returns [`JitError`] when compilation, loading, or program creation
/// fails, when the authored program fails or panics, or when its result
/// payload does not decode as rows.
pub fn run_screening_source(
    workspace: &ManagedWorkspace,
    source: &str,
    parameters: &serde_json::Value,
    context: &ScreenContext,
    rank_metric: &str,
) -> Result<Vec<ScreenRow>, JitError> {
    let artifact = compile(workspace, source, ProgramKind::Screening)?;
    let mut loaded = unsafe { LoadedScreening::load(&artifact.artifact_path, parameters)? };
    let rows = loaded.screen(context)?;
    Ok(rank_rows(rows, rank_metric))
}

/// Order screening rows by `rank_metric` descending, then symbol ascending.
///
/// Metric values are exact decimal strings compared as exact decimals, so
/// values with different scales (`9.5` and `9.50`) tie and fall through to
/// the symbol tiebreak. Rows that lack the metric — or whose value is not an
/// exact decimal — sort last, symbol ascending. Equal keys keep the
/// program's order, which is deterministic by contract, so identical inputs
/// produce byte-identical results.
#[must_use]
pub fn rank_rows(rows: Vec<ScreenRow>, rank_metric: &str) -> Vec<ScreenRow> {
    let mut ranked: Vec<(Option<Decimal>, ScreenRow)> = rows
        .into_iter()
        .map(|row| {
            let value = row
                .metrics
                .get(rank_metric)
                .and_then(|text| text.parse::<Decimal>().ok());
            (value, row)
        })
        .collect();
    ranked.sort_by(|(left_value, left_row), (right_value, right_row)| {
        match (left_value, right_value) {
            (Some(left), Some(right)) => right
                .cmp(left)
                .then_with(|| left_row.symbol.cmp(&right_row.symbol)),
            (Some(_), None) => Ordering::Less,
            (None, Some(_)) => Ordering::Greater,
            (None, None) => left_row.symbol.cmp(&right_row.symbol),
        }
    });
    ranked.into_iter().map(|(_, row)| row).collect()
}
