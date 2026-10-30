//! Immutable bounded tick blocks and borrowed market observations.
//! Hosts own preparation and persistence. Matching and authored adapters consume
//! the same view for owned live observations and block-backed historical ticks.
use crate::program::EngineBar;
use num::{Decimal, Decimal18};
use std::sync::Arc;

pub const MAX_BLOCK_ROWS: usize = 73_728;
pub const MAX_BLOCK_TEXT_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TickKind {
    Quote,
    Trade,
}

/// Numeric columns remain exact scale-18 coefficients; text lives in one arena.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TickRow {
    pub timestamp_ns: i64,
    pub sequence: u64,
    pub values: [Decimal18; 4],
    pub event_id: (u32, u32),
    pub venue: (u32, u32),
    pub trade_id: (u32, u32),
}

#[derive(Debug, PartialEq, Eq)]
pub struct TickBlock {
    symbol: String,
    kind: TickKind,
    rows: Vec<TickRow>,
    text: String,
}
impl TickBlock {
    /// Validate offsets before admitting a decoded block. No native memory is
    /// deserialized or trusted as a Rust struct.
    pub fn new(
        symbol: String,
        kind: TickKind,
        rows: Vec<TickRow>,
        text: String,
    ) -> Result<Self, String> {
        if symbol.is_empty()
            || rows.is_empty()
            || rows.len() > MAX_BLOCK_ROWS
            || text.len() > MAX_BLOCK_TEXT_BYTES
        {
            return Err("invalid tick block bounds".into());
        }
        let valid = |(start, len): (u32, u32)| {
            (start as usize)
                .checked_add(len as usize)
                .is_some_and(|end| text.get(start as usize..end).is_some())
        };
        if rows
            .iter()
            .any(|r| !valid(r.event_id) || !valid(r.venue) || !valid(r.trade_id))
            || rows
                .windows(2)
                .any(|p| (p[0].timestamp_ns, p[0].sequence) > (p[1].timestamp_ns, p[1].sequence))
        {
            return Err("invalid tick block offsets or order".into());
        }
        Ok(Self {
            symbol,
            kind,
            rows,
            text,
        })
    }
    #[inline]
    pub fn symbol(&self) -> &str {
        &self.symbol
    }
    #[inline]
    pub fn kind(&self) -> TickKind {
        self.kind
    }
    #[inline]
    pub fn rows(&self) -> &[TickRow] {
        &self.rows
    }
    #[inline]
    pub fn text(&self) -> &str {
        &self.text
    }
    #[inline]
    pub fn event(self: &Arc<Self>, index: usize) -> Option<BlockEvent> {
        (index < self.rows.len()).then(|| BlockEvent {
            block: self.clone(),
            index,
        })
    }
    fn string(&self, (start, len): (u32, u32)) -> &str {
        &self.text[start as usize..(start + len) as usize]
    }
}

/// An event retains its immutable block across queue lookahead, with no row allocation.
#[derive(Clone, Debug)]
pub struct BlockEvent {
    block: Arc<TickBlock>,
    index: usize,
}
impl PartialEq for BlockEvent {
    fn eq(&self, other: &Self) -> bool {
        self.kind() == other.kind()
            && self.symbol() == other.symbol()
            && self.row().timestamp_ns == other.row().timestamp_ns
            && self.row().sequence == other.row().sequence
            && self.row().values == other.row().values
            && self.event_id() == other.event_id()
            && self.venue() == other.venue()
            && self.trade_id() == other.trade_id()
    }
}
impl Eq for BlockEvent {}
impl BlockEvent {
    #[inline]
    pub fn row(&self) -> &TickRow {
        &self.block.rows[self.index]
    }
    #[inline]
    pub fn symbol(&self) -> &str {
        self.block.symbol()
    }
    #[inline]
    pub fn kind(&self) -> TickKind {
        self.block.kind()
    }
    #[inline]
    pub fn event_id(&self) -> &str {
        self.block.string(self.row().event_id)
    }
    #[inline]
    pub fn venue(&self) -> &str {
        self.block.string(self.row().venue)
    }
    #[inline]
    pub fn trade_id(&self) -> &str {
        self.block.string(self.row().trade_id)
    }
    #[inline]
    pub fn view(&self) -> MarketView<'_> {
        let r = self.row();
        let decimal =
            |n: Decimal18| Decimal::from_parts(n.is_negative(), n.unscaled().unsigned_abs(), 18);
        match self.kind() {
            TickKind::Quote => MarketView::Quote {
                event_id: self.event_id(),
                timestamp_ns: r.timestamp_ns,
                sequence: r.sequence,
                symbol: self.symbol(),
                bid: decimal(r.values[0]),
                ask: decimal(r.values[1]),
                bid_quantity: decimal(r.values[2]),
                ask_quantity: decimal(r.values[3]),
                venue: self.venue(),
            },
            TickKind::Trade => MarketView::Trade {
                event_id: self.event_id(),
                timestamp_ns: r.timestamp_ns,
                sequence: r.sequence,
                symbol: self.symbol(),
                price: decimal(r.values[0]),
                quantity: decimal(r.values[1]),
                venue: self.venue(),
                trade_id: self.trade_id(),
            },
        }
    }
}

/// Callback-scoped strings and exact values, independent of observation storage.
#[derive(Clone, Copy, Debug)]
pub enum MarketView<'a> {
    Quote {
        event_id: &'a str,
        timestamp_ns: i64,
        sequence: u64,
        symbol: &'a str,
        bid: Decimal,
        ask: Decimal,
        bid_quantity: Decimal,
        ask_quantity: Decimal,
        venue: &'a str,
    },
    Trade {
        event_id: &'a str,
        timestamp_ns: i64,
        sequence: u64,
        symbol: &'a str,
        price: Decimal,
        quantity: Decimal,
        venue: &'a str,
        trade_id: &'a str,
    },
    Bar(&'a EngineBar),
}
