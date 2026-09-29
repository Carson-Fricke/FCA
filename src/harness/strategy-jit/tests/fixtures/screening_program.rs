//! Authored screening fixture: derives one row per universe symbol.
//!
//! This source is compiled by the JIT seam inside the tests to a cdylib and
//! run once over a host-built context. It exercises the full screening
//! surface — universe iteration, bars, facts, precomputed indicators, the
//! parameters document, and exact string metrics — and has no order surface
//! by construction.

use std::collections::BTreeMap;

use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::{
    export_screening_program, ScreenContext, ScreenRow, ScreeningProgram,
};

#[derive(Debug, Default)]
struct ContextScreening {
    label: String,
}

impl ScreeningProgram for ContextScreening {
    fn new(parameters: Value) -> Self {
        Self {
            label: parameters
                .get("label")
                .and_then(Value::as_str)
                .unwrap_or("unset")
                .to_string(),
        }
    }

    fn screen(&mut self, ctx: &ScreenContext) -> Vec<ScreenRow> {
        ctx.universe()
            .iter()
            .map(|symbol| {
                let mut metrics = BTreeMap::new();
                if let Some(momentum) = ctx.facts(symbol).get("momentum") {
                    metrics.insert("momentum".to_string(), momentum.clone());
                }
                if let Some(bar) = ctx.bars(symbol).last() {
                    metrics.insert("close".to_string(), bar.close.clone());
                }
                if let Some(value) = ctx.indicators(symbol).get("ind:sma(20)") {
                    metrics.insert("ind:sma(20)".to_string(), value.clone());
                }
                metrics.insert("label".to_string(), self.label.clone());
                ScreenRow {
                    symbol: symbol.clone(),
                    metrics,
                }
            })
            .collect()
    }
}

export_screening_program!(ContextScreening);
