//! Hardware-accelerated SHA-256 with the engine's stable byte and hex interface.
use sha2::Digest;
pub const DIGEST_LEN: usize = 32;
#[derive(Clone, Debug, Default)]
pub struct Sha256(sha2::Sha256);
impl Sha256 {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn update(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }
    #[must_use]
    pub fn finalize(self) -> [u8; DIGEST_LEN] {
        self.0.finalize().into()
    }
    #[must_use]
    pub fn finalize_hex(self) -> String {
        hex(&self.finalize())
    }
}

/// Hash a byte slice in one call.
#[must_use]
pub fn digest(bytes: &[u8]) -> [u8; DIGEST_LEN] {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize()
}

/// Hash a byte slice and return the lowercase hexadecimal digest.
#[must_use]
pub fn digest_hex(bytes: &[u8]) -> String {
    hex(&digest(bytes))
}

/// Render a digest as lowercase hexadecimal.
#[must_use]
pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(char::from(DIGITS[(byte >> 4) as usize]));
        out.push(char::from(DIGITS[(byte & 0x0f) as usize]));
    }
    out
}
