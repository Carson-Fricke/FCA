//! Execution venue boundary for the synchronous harness kernel.
//!
//! Fire-and-forget execution commands whose outcomes arrive as events.
//! Implementations publish execution reports, fills, and position snapshots;
//! command return values never carry venue outcomes, which keeps simulated and
//! live adapters on the same boundary.

use harness_trading::error::LiveResult;
use harness_trading::orders::OrderRequest;

/// The synchronous venue command boundary.
pub trait ExecutionVenue {
    /// Submit an order and return before any outcome is delivered.
    fn submit(&mut self, order: OrderRequest) -> LiveResult<()>;

    /// Request cancellation; confirmation or rejection arrives as an event.
    fn cancel(&mut self, order_id: &str) -> LiveResult<()>;

    /// Request atomic replacement of an existing order.
    fn replace(&mut self, order_id: &str, replacement: OrderRequest) -> LiveResult<()>;

    /// Request current positions for an account through the event bus.
    fn request_position_snapshot(&mut self, account_id: &str) -> LiveResult<()>;
}
