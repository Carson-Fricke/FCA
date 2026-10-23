//! Owns touch and stop-trigger policy for simulated execution in both runtimes.
//! Callers supply observed prices and retain stop activation; no clock or I/O.
use crate::orders::OrderType;
use crate::orders::Side;

pub fn stop_triggered<T: Copy + PartialOrd>(side: Side, stop: T, price: T) -> bool {
    match side {
        Side::Buy => price >= stop,
        Side::Sell => price <= stop,
    }
}

/// Return sticky activation and a possible fill price. Trades fill market/stop
/// orders; quotes additionally match limits. A stop-limit can trigger before touch.
pub fn touch<T: Copy + PartialOrd>(
    side: Side,
    kind: OrderType,
    limit: Option<T>,
    stop: Option<T>,
    price: T,
    quote: bool,
    activated: bool,
) -> (bool, Option<T>) {
    let activated = activated
        || !matches!(kind, OrderType::Stop | OrderType::StopLimit)
        || stop.is_some_and(|stop| stop_triggered(side, stop, price));
    if !activated {
        return (false, None);
    }
    let fill = match kind {
        OrderType::Market | OrderType::Stop => Some(price),
        OrderType::Limit | OrderType::StopLimit if quote => limit
            .filter(|limit| match side {
                Side::Buy => price <= *limit,
                Side::Sell => price >= *limit,
            })
            .map(|_| price),
        _ => None,
    };
    (activated, fill)
}
