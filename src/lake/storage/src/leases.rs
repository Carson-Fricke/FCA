//! Scoped leases for protecting symbol-day lake replacements.
//!
//! Ports the provider-independent half of `lake.storage.leases`: the
//! canonicalized [`SymbolDayScope`] key and Azure lock-blob name, the [`Lease`]
//! and [`LeaseProvider`] boundaries, and the `hold_lease` helper. The Azure
//! provider itself is deferred to the hosted phase.

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

    /// Return the deterministic relative name used by Azure lock blobs.
    pub fn blob_name(&self) -> String {
        format!(
            "date={}/asset_class={}/{}.lock",
            self.session_date.isoformat(),
            self.asset_class.value(),
            quote(&self.symbol)
        )
    }
}

/// A held scope lease with explicit renewal and release operations.
pub trait Lease: Send + Sync {
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
