//! Fenced ownership of symbol-day coverage manifests.
//!
//! Ports the provider-independent half of `lake.storage.leases`: the
//! Canonical manifest identity, renewal cadence and fenced read/publish ownership
//! shared by local kernel locks and Azure blob leases.

use lake_contracts::{symbol_filename, AssetClass, DataError, Date};

/// The shared lock identity for one asset, symbol, and session date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SymbolDayScope {
    /// The lake asset class.
    pub asset_class: AssetClass,
    /// The canonical uppercase symbol.
    pub symbol: String,
    /// The session date.
    pub session_date: Date,
}

impl SymbolDayScope {
    /// Canonicalize the symbol and build the scope.
    pub fn new(
        asset_class: AssetClass,
        symbol: &str,
        session_date: Date,
    ) -> Result<Self, DataError> {
        let canonical = symbol.trim().to_uppercase();
        symbol_filename(&canonical)?;
        Ok(Self {
            asset_class,
            symbol: canonical,
            session_date,
        })
    }

    /// Return a stable, provider-independent scope key.
    pub fn key(&self) -> String {
        format!(
            "{}/{}/{}",
            self.asset_class.value(),
            self.session_date.isoformat(),
            quote(&self.symbol)
        )
    }

    /// Return the deterministic manifest name protected by both adapters.
    pub fn blob_name(&self) -> String {
        format!(
            "asset_class={}/symbol={}/date={}.json",
            self.asset_class.value(),
            quote(&self.symbol),
            self.session_date.isoformat()
        )
    }
}

/// A held scope lease with explicit renewal and release operations.
pub trait Lease: Send + Sync {
    /// Finite owners supply a renewal cadence; kernel ownership has no expiry.
    fn renewal_interval(&self) -> Option<std::time::Duration>;
    /// Read the manifest protected by this ownership token.
    fn read(&self) -> Result<Vec<u8>, DataError>;

    /// Atomically replace the manifest, rejecting expired or released ownership.
    fn publish(&self, payload: &[u8]) -> Result<(), DataError>;
    /// Return the provider lease identifier.
    fn lease_id(&self) -> Option<&str>;

    /// Renew the lease while it is still held.
    fn renew(&self) -> Result<(), DataError>;

    /// Release the lease; successful releases are idempotent.
    fn release(&self) -> Result<(), DataError>;
}

/// Injectable source of leases for symbol-day scopes.
pub trait LeaseProvider: Send + Sync {
    /// Acquire the scope or fail when another writer owns it.
    fn acquire(&self, scope: &SymbolDayScope) -> Result<Box<dyn Lease>, DataError>;
}

/// Percent-encode a canonical symbol like Python's `quote(symbol, safe="._-")`.
fn quote(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    encoded
}
