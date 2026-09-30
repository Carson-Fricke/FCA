//! Deterministic identifier source.
//!
//! Port of `harness.core.ids.EngineIdSource`. Identifiers are SHA-256 derived
//! from the seed, the kind, and a monotonic ordinal; the first 16 digest bytes
//! are tagged as a UUID v5-shaped value. No clock or random source participates.

use crate::sha256::Sha256;

use super::config::Seed;

/// Issue reproducible UUID-shaped identifier strings from a seed and counter.
#[derive(Clone, Debug)]
pub struct EngineIdSource {
    seed: Seed,
    counter: u64,
}

impl EngineIdSource {
    /// Create a source for `seed`.
    ///
    /// # Errors
    ///
    /// Returns [`IdError::EmptySeed`] when a textual or byte seed is empty.
    pub fn new(seed: Seed) -> Result<Self, IdError> {
        match &seed {
            Seed::Text(value) if value.is_empty() => return Err(IdError::EmptySeed),
            Seed::Bytes(value) if value.is_empty() => return Err(IdError::EmptySeed),
            _ => {}
        }
        Ok(Self { seed, counter: 0 })
    }

    /// Return the number of identifiers issued by this source.
    #[must_use]
    pub const fn issued(&self) -> u64 {
        self.counter
    }

    /// Issue the next deterministic identifier for `kind`.
    ///
    /// # Errors
    ///
    /// Returns [`IdError::EmptyKind`] when `kind` is empty.
    pub fn next_id(&mut self, kind: &str) -> Result<String, IdError> {
        if kind.is_empty() {
            return Err(IdError::EmptyKind);
        }
        let ordinal = self.counter;
        self.counter += 1;
        let seed_bytes = self.seed_bytes();
        let mut hasher = Sha256::new();
        hasher.update(&seed_bytes);
        hasher.update(&[0]);
        hasher.update(kind.as_bytes());
        hasher.update(&[0]);
        hasher.update(&u128::from(ordinal).to_be_bytes());
        let mut digest = hasher.finalize();
        digest[6] = (digest[6] & 0x0f) | 0x50;
        digest[8] = (digest[8] & 0x3f) | 0x80;
        Ok(format_uuid(&digest[..16]))
    }

    fn seed_bytes(&self) -> Vec<u8> {
        match &self.seed {
            Seed::Text(value) => format!("str:{value}").into_bytes(),
            Seed::Int(value) => format!("int:{value}").into_bytes(),
            Seed::Bytes(value) => value.clone(),
        }
    }
}

/// Identifier derivation failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IdError {
    /// The identifier kind was empty.
    EmptyKind,
    /// The seed was empty.
    EmptySeed,
}

impl std::fmt::Display for IdError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyKind => formatter.write_str("id kind must be a non-empty string"),
            Self::EmptySeed => formatter.write_str("id seed must not be empty"),
        }
    }
}

impl std::error::Error for IdError {}

/// Format 16 bytes as a canonical lowercase UUID string.
#[must_use]
pub fn format_uuid(bytes: &[u8]) -> String {
    debug_assert_eq!(bytes.len(), 16);
    let hex = crate::sha256::hex(bytes);
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}
