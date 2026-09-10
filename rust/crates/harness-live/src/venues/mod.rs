//! Simulated and live execution-venue capabilities.
//!
//! Ports `harness.venues`: the fire-and-forget venue boundary, the scripted
//! and simulated venues, atomic options matching, and the Alpaca adapter
//! shells. Live venue I/O is isolated behind async traits and `tokio`; the
//! synchronous kernel never touches the clock or the network.

pub mod alpaca;
pub mod alpaca_crypto;
pub mod alpaca_options;
pub mod base;
pub mod options_matching;
pub mod plugin;
pub mod scripted;
pub mod sim;

pub use alpaca::{
    AlpacaBroker, AlpacaClientOrderIdSource, AlpacaCommandReceipt, AlpacaExecutionInbox,
    AlpacaOrderBinding, AlpacaOrderDirectory, AsyncBrokerTransport, TransportResponse,
};
pub use alpaca_crypto::{
    is_crypto_pair, AlpacaCryptoFeeSchedule, AlpacaCryptoVenue, CryptoFeeLedgerEntry,
};
pub use alpaca_options::{
    AlpacaOptionsCapabilityMatrix, CapabilityRejection, UnsupportedFeatureReason, VenueCapability,
    VenueCapabilitySupport,
};
pub use base::ExecutionVenue;
pub use options_matching::OptionsMatchingEngine;
pub use plugin::FillPlugin;
pub use scripted::{
    ScriptedStep, ScriptedVenue, UnexpectedVenueCall, VenueCall, VenueOperation, VenueOutcome,
};
pub use sim::{SimOrderState, SimulatedVenue};
