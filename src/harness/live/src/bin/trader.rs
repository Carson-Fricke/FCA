//! Deterministic trader entrypoint over the Rust live core.
//!
//! Runs one scripted session: stage a quote, submit a market order, and print
//! the resulting OMS lifecycle, position, and cash. There is no network,
//! wall-clock, or random access on this path.

use std::error::Error;

use live::events::{MarketEvent, QuoteEvent};
use live::orders::{OrderType, Side, SingleLegOrder, TimeInForce};
use live::risk::limits::MaxOrderNotional;
use live::risk::models::RiskRule;
use live::risk::pipeline::OnReject;
use live::symbol::Symbol;
use live::trader::{SubmitOutcome, TraderConfig, TraderCore};
use num::Decimal18;

fn dec(value: &str) -> Result<Decimal18, Box<dyn Error>> {
    Ok(value.parse()?)
}

fn main() -> Result<(), Box<dyn Error>> {
    let config = TraderConfig {
        account_id: "paper-account".to_string(),
        book_id: "strategy-book".to_string(),
        initial_cash: dec("100000")?,
        venue: "sim".to_string(),
    };
    let rules: Vec<Box<dyn RiskRule>> = vec![Box::new(MaxOrderNotional::new(
        "max-notional",
        dec("100000")?,
    )?)];
    let mut trader = TraderCore::new(config, rules, OnReject::RejectOrder)?;

    let symbol = Symbol::new("AAPL")?;
    trader.on_market(MarketEvent::Quote(QuoteEvent::new(
        symbol.clone(),
        dec("100")?,
        dec("50")?,
        dec("101")?,
        dec("50")?,
        Some("sim".to_string()),
    )?))?;

    let order = SingleLegOrder::new(
        "demo-order",
        symbol,
        Side::Buy,
        dec("10")?,
        OrderType::Market,
        TimeInForce::Day,
        None,
        None,
        None,
    )?;

    match trader.submit(order)? {
        SubmitOutcome::Approved(snapshot) => {
            println!(
                "approved order={} status={} filled={}",
                snapshot.order_id(),
                snapshot.status.as_str(),
                snapshot.filled_quantity
            );
        }
        SubmitOutcome::Rejected(decision) => {
            println!("rejected reason={:?}", decision.reason);
        }
    }
    let position = trader.position("AAPL")?;
    println!("position={} cash={}", position.quantity, trader.cash()?);
    Ok(())
}
