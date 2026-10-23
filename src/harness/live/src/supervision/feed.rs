//! Owns trader supervision feed; lifecycle coordination remains in the parent.
use std::rc::Rc;

use super::models::*;
use crate::venues::transport::RetryConfig;
use harness_trading::events::MarketEvent;

/// A market-data feed fault.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FeedError {
    /// The feed dropped; reconnect with backoff.
    Disconnected(String),
    /// The feed failed terminally.
    Fatal(String),
}

/// The synchronous market-data boundary the supervisor drains.
pub trait MarketFeed {
    /// Return the next event, or `None` at the session close.
    ///
    /// # Errors
    ///
    /// Returns [`FeedError`] when the feed drops or fails.
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError>;
}

/// A feed factory invoked for every connect attempt.
pub type FeedFactory = Box<dyn FnMut() -> Result<Box<dyn MarketFeed>, FeedError>>;

/// A replay/scripted feed over an in-memory event queue.
#[derive(Debug, Default)]
pub struct ScriptedMarketFeed {
    events: std::collections::VecDeque<MarketEvent>,
}

impl ScriptedMarketFeed {
    /// Build a feed over an ordered event script.
    #[must_use]
    pub fn new(events: Vec<MarketEvent>) -> Self {
        Self {
            events: events.into(),
        }
    }
}

impl MarketFeed for ScriptedMarketFeed {
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError> {
        Ok(self.events.pop_front())
    }
}

/// A feed that immediately reaches the session close.
#[derive(Debug, Default)]
pub struct EmptyMarketFeed;

impl MarketFeed for EmptyMarketFeed {
    fn next_event(&mut self) -> Result<Option<MarketEvent>, FeedError> {
        Ok(None)
    }
}

/// Supervised feed that reconnects on disconnect with exponential backoff.
///
/// The reconnect schedule reuses the live transport [`RetryConfig`], so the
/// supervisor, the REST retry wrapper, and the WebSocket boundary all share one
/// backoff policy. The sleeper is injected, so tests never block.
pub struct SupervisedFeed {
    factory: FeedFactory,
    current: Option<Box<dyn MarketFeed>>,
    policy: RetryConfig,
    sleeper: Rc<dyn Fn(i64)>,
    reconnects: u64,
    drops: u64,
}

impl SupervisedFeed {
    /// Build a supervised feed with an explicit backoff policy.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Configuration`] when the policy is invalid.
    pub fn new(
        factory: FeedFactory,
        connect_attempts: u32,
        connect_backoff_ns: i64,
        sleeper: Rc<dyn Fn(i64)>,
    ) -> Result<Self, SupervisionError> {
        let attempts = connect_attempts.max(1) as usize;
        let backoff_seconds = connect_backoff_ns.max(0) as f64 / 1_000_000_000.0;
        let policy = RetryConfig::new(attempts, backoff_seconds, 30.0, false, 30.0)
            .map_err(|error| SupervisionError::Configuration(error.to_string()))?;
        Ok(Self {
            factory,
            current: None,
            policy,
            sleeper,
            reconnects: 0,
            drops: 0,
        })
    }

    /// The number of reconnects performed.
    #[must_use]
    pub const fn reconnects(&self) -> u64 {
        self.reconnects
    }

    /// The number of dropped connections observed.
    #[must_use]
    pub const fn drops(&self) -> u64 {
        self.drops
    }

    /// Connect once, retrying with backoff up to the configured attempts.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Restartable`] when every attempt fails.
    pub fn connect(&mut self) -> Result<(), SupervisionError> {
        let mut last = String::from("no attempt was made");
        for attempt in 1..=self.policy.max_attempts {
            match (self.factory)() {
                Ok(feed) => {
                    self.current = Some(feed);
                    return Ok(());
                }
                Err(error) => {
                    let (FeedError::Disconnected(message) | FeedError::Fatal(message)) = error;
                    last = message;
                    if attempt < self.policy.max_attempts {
                        let delay_ns = (self.policy.backoff(attempt) * 1_000_000_000.0) as i64;
                        (self.sleeper)(delay_ns);
                    }
                }
            }
        }
        Err(SupervisionError::Restartable(format!(
            "market-data boundary could not be composed after {} attempts: {last}",
            self.policy.max_attempts
        )))
    }

    /// Return the next event, reconnecting transparently on a drop.
    ///
    /// # Errors
    ///
    /// Returns [`SupervisionError::Restartable`] when reconnects are exhausted
    /// and [`SupervisionError::Fault`] on a terminal feed fault.
    pub fn next_event(&mut self) -> Result<Option<MarketEvent>, SupervisionError> {
        loop {
            if self.current.is_none() {
                self.connect()?;
            }
            let feed = self
                .current
                .as_mut()
                .ok_or_else(|| SupervisionError::Fault("feed was not connected".to_string()))?;
            match feed.next_event() {
                Ok(option) => return Ok(option),
                Err(FeedError::Fatal(message)) => return Err(SupervisionError::Fault(message)),
                Err(FeedError::Disconnected(_message)) => {
                    self.drops += 1;
                    self.current = None;
                    self.reconnects += 1;
                    self.connect()?;
                }
            }
        }
    }
}
