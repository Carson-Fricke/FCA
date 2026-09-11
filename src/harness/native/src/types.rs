//! Core domain types for the native harness core.
//!
//! Ports the deterministic core from `types.zig`: canonical market symbols,
//! seeded deterministic identifiers, canonical (byte-identical) decimal text
//! shared with the Python codecs, UTC ISO-8601 timestamps, and canonical trade
//! row serialization.
//!
//! Determinism rules: no wall-clock, no unseeded random, no network. Every
//! routine is a pure function of its inputs. Errors are returned, never
//! panicked, so no panic can cross the FFI boundary.

use crate::sha256::Sha256;

/// Version of the stable C ABI exported by the native core. The Python loader
/// (`src/harness/_native.py`) pins the same value and refuses to load a library
/// that disagrees.
pub const ABI_VERSION: u32 = 2;

/// Pinned fixed-point scale for native decimals (`pa.decimal128(38, 18)`).
pub const DECIMAL_SCALE: u32 = 18;

/// Pinned decimal precision for native decimals.
pub const DECIMAL_PRECISION: u32 = 38;

/// Largest `|unscaled|` value representable by `decimal128(38, 18)`.
pub const MAX_UNSCALED: u128 = 99_999_999_999_999_999_999_999_999_999_999_999_999;

/// A checked failure from a core domain routine.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TypeError {
    /// A required argument was empty, null, or otherwise invalid.
    InvalidArgument,
    /// The symbol was not a valid canonical market or OSI symbol.
    InvalidSymbol,
    /// The decimal value or scale is outside the pinned domain.
    InvalidDecimal,
    /// The timestamp is not microsecond-aligned or is outside the domain.
    InvalidTimestamp,
    /// The caller-provided output buffer was too small.
    BufferTooSmall,
    /// The value is outside its representable range.
    OutOfRange,
}

impl core::fmt::Display for TypeError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::InvalidArgument => "invalid argument",
            Self::InvalidSymbol => "invalid symbol",
            Self::InvalidDecimal => "invalid decimal",
            Self::InvalidTimestamp => "invalid timestamp",
            Self::BufferTooSmall => "buffer too small",
            Self::OutOfRange => "out of range",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for TypeError {}

/// The detected kind of a canonicalized symbol.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SymbolKind {
    /// A plain market symbol (`AAPL`, `BRK.B`).
    Market,
    /// An OSI option symbol (`AAPL240119C00150000`).
    Osi,
}

fn is_ascii_whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

fn is_ascii_digit(byte: u8) -> bool {
    byte.is_ascii_digit()
}

fn is_symbol_body(byte: u8) -> bool {
    byte.is_ascii_uppercase()
        || byte.is_ascii_digit()
        || matches!(byte, b'.' | b'/' | b':' | b'_' | b'-')
}

fn is_osi_root(byte: u8) -> bool {
    byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'.'
}

fn matches_market_symbol(value: &[u8]) -> bool {
    match value.split_first() {
        None => false,
        Some((&first, rest)) => {
            if !(first.is_ascii_uppercase() || is_ascii_digit(first)) {
                return false;
            }
            rest.iter().all(|&byte| is_symbol_body(byte))
        }
    }
}

fn matches_osi_symbol(value: &[u8]) -> bool {
    // ^[A-Z0-9.]{1,6}\d{6}[CP]\d{8}$ — total length 16..=21 depending on the
    // root length. The Python regex backtracks the root, so scan every
    // possible split point instead of consuming the root greedily.
    if !(16..=21).contains(&value.len()) {
        return false;
    }
    for root_len in 1..=6usize {
        if value.len() != root_len + 15 {
            continue;
        }
        if !value[..root_len].iter().all(|&byte| is_osi_root(byte)) {
            continue;
        }
        let rest = &value[root_len..];
        if !rest[..6].iter().all(|&byte| is_ascii_digit(byte)) {
            continue;
        }
        if rest[6] != b'C' && rest[6] != b'P' {
            continue;
        }
        if rest[7..15].iter().all(|&byte| is_ascii_digit(byte)) {
            return true;
        }
    }
    false
}

/// Canonicalize a market or OSI symbol exactly like
/// `harness.core.symbols.canonical_symbol`: strip surrounding whitespace,
/// uppercase ASCII, compact a space-padded OSI root, and validate.
///
/// On success returns the length of the canonical bytes written to `out` and
/// the detected kind.
pub fn canonical_symbol(input: &[u8], out: &mut [u8]) -> Result<(usize, SymbolKind), TypeError> {
    if out.len() < input.len() {
        return Err(TypeError::BufferTooSmall);
    }
    let mut start = 0usize;
    let mut end = input.len();
    while start < end && is_ascii_whitespace(input[start]) {
        start += 1;
    }
    while end > start && is_ascii_whitespace(input[end - 1]) {
        end -= 1;
    }
    let trimmed = &input[start..end];
    if trimmed.is_empty() {
        return Err(TypeError::InvalidSymbol);
    }
    for (index, &byte) in trimmed.iter().enumerate() {
        out[index] = if byte.is_ascii_lowercase() {
            byte - 32
        } else {
            byte
        };
    }
    let len = trimmed.len();
    if len >= 21 && out[..6].contains(&b' ') {
        let mut keep = 6usize;
        while keep > 0 && out[keep - 1] == b' ' {
            keep -= 1;
        }
        out.copy_within(6..len, keep);
        let compacted_len = keep + (len - 6);
        // A compacted candidate was an OSI attempt; Python falls back to the
        // market regex on the *original* value, which the space would reject.
        if matches_osi_symbol(&out[..compacted_len]) {
            return Ok((compacted_len, SymbolKind::Osi));
        }
        return Err(TypeError::InvalidSymbol);
    }
    if matches_osi_symbol(&out[..len]) {
        return Ok((len, SymbolKind::Osi));
    }
    if matches_market_symbol(&out[..len]) {
        return Ok((len, SymbolKind::Market));
    }
    Err(TypeError::InvalidSymbol)
}

/// Derive a deterministic UUID (raw 16 bytes) exactly like `harness.core.ids`:
/// `sha256(seed_bytes || 0x00 || kind || 0x00 || ordinal_be_u16bytes)`, with the
/// UUID version/variant nibbles pinned to version 5.
pub fn deterministic_id(
    seed: &[u8],
    kind: &[u8],
    ordinal: u64,
    out: &mut [u8; 16],
) -> Result<(), TypeError> {
    if seed.is_empty() || kind.is_empty() {
        return Err(TypeError::InvalidArgument);
    }
    let mut hasher = Sha256::new();
    hasher.update(seed);
    hasher.update(&[0]);
    hasher.update(kind);
    hasher.update(&[0]);
    let mut ordinal_bytes = [0u8; 16];
    ordinal_bytes[8..16].copy_from_slice(&ordinal.to_be_bytes());
    hasher.update(&ordinal_bytes);
    let digest = hasher.finalize();
    out.copy_from_slice(&digest[..16]);
    out[6] = (out[6] & 0x0F) | 0x50;
    out[8] = (out[8] & 0x3F) | 0x80;
    Ok(())
}

/// Checked, allocation-free output buffer writer. Every overflow surfaces as
/// [`TypeError::BufferTooSmall`]; nothing panics.
struct Writer<'a> {
    buf: &'a mut [u8],
    index: usize,
}

impl<'a> Writer<'a> {
    fn new(buf: &'a mut [u8]) -> Self {
        Self { buf, index: 0 }
    }

    fn written(&self) -> usize {
        self.index
    }

    fn write_byte(&mut self, byte: u8) -> Result<(), TypeError> {
        if self.index >= self.buf.len() {
            return Err(TypeError::BufferTooSmall);
        }
        self.buf[self.index] = byte;
        self.index += 1;
        Ok(())
    }

    fn write_byte_n(&mut self, byte: u8, count: usize) -> Result<(), TypeError> {
        if self.index + count > self.buf.len() {
            return Err(TypeError::BufferTooSmall);
        }
        self.buf[self.index..self.index + count].fill(byte);
        self.index += count;
        Ok(())
    }

    fn write_all(&mut self, bytes: &[u8]) -> Result<(), TypeError> {
        if self.index + bytes.len() > self.buf.len() {
            return Err(TypeError::BufferTooSmall);
        }
        self.buf[self.index..self.index + bytes.len()].copy_from_slice(bytes);
        self.index += bytes.len();
        Ok(())
    }

    fn write_int(&mut self, value: i64) -> Result<(), TypeError> {
        let mut buffer = [0u8; 20];
        let mut end = buffer.len();
        let negative = value < 0;
        let mut magnitude = value.unsigned_abs();
        loop {
            end -= 1;
            buffer[end] = b'0' + (magnitude % 10) as u8;
            magnitude /= 10;
            if magnitude == 0 {
                break;
            }
        }
        if negative {
            end -= 1;
            buffer[end] = b'-';
        }
        self.write_all(&buffer[end..])
    }

    fn write_uint(&mut self, value: u64) -> Result<(), TypeError> {
        let mut buffer = [0u8; 20];
        let mut end = buffer.len();
        let mut magnitude = value;
        loop {
            end -= 1;
            buffer[end] = b'0' + (magnitude % 10) as u8;
            magnitude /= 10;
            if magnitude == 0 {
                break;
            }
        }
        self.write_all(&buffer[end..])
    }

    fn write_int_padded(&mut self, value: u64, width: usize) -> Result<(), TypeError> {
        let mut buffer = [0u8; 20];
        let mut end = buffer.len();
        let mut magnitude = value;
        loop {
            end -= 1;
            buffer[end] = b'0' + (magnitude % 10) as u8;
            magnitude /= 10;
            if magnitude == 0 {
                break;
            }
        }
        let rendered = &buffer[end..];
        if rendered.len() > width {
            return Err(TypeError::BufferTooSmall);
        }
        self.write_byte_n(b'0', width - rendered.len())?;
        self.write_all(rendered)
    }
}

/// Render the magnitude as decimal digits without leading zeros; zero renders
/// as a single `0`.
fn decimal_digits(magnitude: u128, buf: &mut [u8; 39]) -> usize {
    if magnitude == 0 {
        buf[0] = b'0';
        return 1;
    }
    let mut value = magnitude;
    let mut index = buf.len();
    while value != 0 {
        index -= 1;
        buf[index] = b'0' + (value % 10) as u8;
        value /= 10;
    }
    let count = buf.len() - index;
    buf.copy_within(index.., 0);
    count
}

/// Return the canonical decimal text for a value carried at a fixed scale,
/// byte-identical to Python's `str(Decimal(unscaled).scaleb(-scale))`.
pub fn decimal_text(unscaled: i128, scale: u32, out: &mut [u8]) -> Result<usize, TypeError> {
    if scale > DECIMAL_PRECISION {
        return Err(TypeError::InvalidDecimal);
    }
    let negative = unscaled < 0;
    let magnitude = unscaled.unsigned_abs();
    if magnitude > MAX_UNSCALED {
        return Err(TypeError::InvalidDecimal);
    }

    let mut digits_buf = [0u8; 39];
    let count = decimal_digits(magnitude, &mut digits_buf);
    let exponent = -(i64::from(scale));
    let ndigits = count as i64;
    let adjusted = exponent + ndigits - 1;

    let mut writer = Writer::new(out);
    if negative {
        writer.write_byte(b'-')?;
    }
    if exponent <= 0 && adjusted >= -6 {
        // Plain notation: digits with the decimal point `scale` places from the
        // right, padded with leading zeros after "0." when needed.
        let point = ndigits + exponent;
        if point <= 0 {
            writer.write_all(b"0.")?;
            writer.write_byte_n(b'0', (-point) as usize)?;
            writer.write_all(&digits_buf[..count])?;
        } else if point >= ndigits {
            writer.write_all(&digits_buf[..count])?;
            writer.write_byte_n(b'0', (point - ndigits) as usize)?;
        } else {
            let point = point as usize;
            writer.write_all(&digits_buf[..point])?;
            writer.write_byte(b'.')?;
            writer.write_all(&digits_buf[point..count])?;
        }
    } else {
        // Scientific notation: first digit, optional fraction, "E", signed
        // adjusted exponent. Python renders non-negative exponents with '+'.
        writer.write_byte(digits_buf[0])?;
        if count > 1 {
            writer.write_byte(b'.')?;
            writer.write_all(&digits_buf[1..count])?;
        }
        writer.write_byte(b'E')?;
        if adjusted >= 0 {
            writer.write_byte(b'+')?;
        }
        writer.write_int(adjusted)?;
    }
    Ok(writer.written())
}

/// A canonical trade row, mirroring the trade row produced by
/// `harness.core.types.codecs`.
pub struct TradeRow<'a> {
    /// Canonical event identifier (non-empty).
    pub event_id: &'a str,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
    /// Canonical market or OSI symbol.
    pub symbol: &'a str,
    /// Unscaled scale-18 price.
    pub price_unscaled: i128,
    /// Unscaled scale-18 quantity.
    pub quantity_unscaled: i128,
    /// Optional venue identifier.
    pub venue: Option<&'a str>,
    /// Optional trade identifier.
    pub trade_id: Option<&'a str>,
}

/// Validate that the symbol is already canonical (the codecs always see
/// canonicalized symbols). Validation uses a local buffer.
fn require_canonical_symbol(symbol: &str) -> Result<(), TypeError> {
    if symbol.is_empty() {
        return Err(TypeError::InvalidArgument);
    }
    let bytes = symbol.as_bytes();
    let mut buffer = [0u8; 256];
    if bytes.len() > buffer.len() {
        return Err(TypeError::InvalidSymbol);
    }
    let (len, _) = canonical_symbol(bytes, &mut buffer).map_err(|_| TypeError::InvalidSymbol)?;
    if &buffer[..len] != bytes {
        return Err(TypeError::InvalidSymbol);
    }
    Ok(())
}

fn require_canonical_text(value: &str) -> Result<(), TypeError> {
    if value.is_empty() {
        return Err(TypeError::InvalidArgument);
    }
    if value.as_bytes().contains(&0) {
        return Err(TypeError::InvalidArgument);
    }
    Ok(())
}

const CIVIL_DAY_SECONDS: i128 = 86_400_000_000;

/// Render an epoch-nanosecond instant as a UTC `datetime.isoformat()` string
/// (`YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`). Nanosecond precision below one
/// microsecond is rejected because the Python domain type is a `datetime`.
pub fn isoformat_utc(timestamp_ns: i128, out: &mut [u8]) -> Result<usize, TypeError> {
    if timestamp_ns.rem_euclid(1_000) != 0 {
        return Err(TypeError::InvalidTimestamp);
    }
    let micros = timestamp_ns / 1_000;
    let days = micros.div_euclid(CIVIL_DAY_SECONDS);
    let micros_of_day = (micros - days * CIVIL_DAY_SECONDS) as u64;
    let day_seconds = (micros_of_day / 1_000_000) as u32;
    let microsecond = (micros_of_day % 1_000_000) as u32;

    let civil = civil_from_days(days);
    if !(1..=9999).contains(&civil.year) {
        return Err(TypeError::InvalidTimestamp);
    }

    let mut writer = Writer::new(out);
    writer.write_int_padded(civil.year as u64, 4)?;
    writer.write_byte(b'-')?;
    writer.write_int_padded(civil.month, 2)?;
    writer.write_byte(b'-')?;
    writer.write_int_padded(civil.day, 2)?;
    writer.write_byte(b'T')?;
    writer.write_int_padded(u64::from(day_seconds / 3600), 2)?;
    writer.write_byte(b':')?;
    writer.write_int_padded(u64::from((day_seconds % 3600) / 60), 2)?;
    writer.write_byte(b':')?;
    writer.write_int_padded(u64::from(day_seconds % 60), 2)?;
    if microsecond != 0 {
        writer.write_byte(b'.')?;
        writer.write_int_padded(u64::from(microsecond), 6)?;
    }
    writer.write_all(b"+00:00")?;
    Ok(writer.written())
}

struct Civil {
    year: i64,
    month: u64,
    day: u64,
}

/// Howard Hinnant's civil-from-days algorithm over `i128` day counts.
fn civil_from_days(days_since_epoch: i128) -> Civil {
    let z = days_since_epoch + 719_468;
    let era = z.div_euclid(146_097);
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = i128::from(yoe) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    Civil {
        year: (year + if month <= 2 { 1 } else { 0 }) as i64,
        month,
        day,
    }
}

/// Serialize a canonical trade row to canonical JSON bytes. Every string must
/// already be canonical (non-empty for identifiers; the symbol must equal its
/// own canonical form).
pub fn serialize_trade_row(row: &TradeRow<'_>, out: &mut [u8]) -> Result<usize, TypeError> {
    if row.event_id.is_empty() {
        return Err(TypeError::InvalidArgument);
    }
    require_canonical_symbol(row.symbol)?;
    require_canonical_text(row.event_id)?;
    if let Some(venue) = row.venue {
        require_canonical_text(venue)?;
    }
    if let Some(trade_id) = row.trade_id {
        require_canonical_text(trade_id)?;
    }

    let mut writer = Writer::new(out);
    writer.write_all(b"{\"event_id\":")?;
    write_json_string(&mut writer, row.event_id.as_bytes())?;
    writer.write_all(b",\"timestamp\":")?;
    let mut ts_buf = [0u8; 35];
    let timestamp = isoformat_utc(row.timestamp_ns, &mut ts_buf)?;
    write_json_string(&mut writer, &ts_buf[..timestamp])?;
    writer.write_all(b",\"sequence\":")?;
    writer.write_uint(row.sequence)?;
    writer.write_all(b",\"symbol\":")?;
    write_json_string(&mut writer, row.symbol.as_bytes())?;
    writer.write_all(b",\"price\":")?;
    let mut price_buf = [0u8; 64];
    let price = decimal_text(row.price_unscaled, DECIMAL_SCALE, &mut price_buf)?;
    write_json_string(&mut writer, &price_buf[..price])?;
    writer.write_all(b",\"quantity\":")?;
    let mut qty_buf = [0u8; 64];
    let quantity = decimal_text(row.quantity_unscaled, DECIMAL_SCALE, &mut qty_buf)?;
    write_json_string(&mut writer, &qty_buf[..quantity])?;
    writer.write_all(b",\"venue\":")?;
    match row.venue {
        Some(venue) => write_json_string(&mut writer, venue.as_bytes())?,
        None => writer.write_all(b"null")?,
    }
    writer.write_all(b",\"trade_id\":")?;
    match row.trade_id {
        Some(trade_id) => write_json_string(&mut writer, trade_id.as_bytes())?,
        None => writer.write_all(b"null")?,
    }
    writer.write_byte(b'}')?;
    Ok(writer.written())
}

/// Write a JSON string exactly like `json.dumps(..., ensure_ascii=True)`:
/// literal U+0020..U+007E, short escapes for control characters, and lowercase
/// `\uXXXX` (with surrogate pairs) for everything else.
fn write_json_string(writer: &mut Writer<'_>, value: &[u8]) -> Result<(), TypeError> {
    writer.write_byte(b'"')?;
    let mut index = 0usize;
    while index < value.len() {
        let byte = value[index];
        if byte < 0x80 {
            write_json_ascii(writer, byte)?;
            index += 1;
            continue;
        }
        let codepoint = decode_utf8(value, index)?;
        index += utf8_sequence_len(byte);
        write_json_codepoint(writer, codepoint)?;
    }
    writer.write_byte(b'"')
}

fn write_json_ascii(writer: &mut Writer<'_>, byte: u8) -> Result<(), TypeError> {
    match byte {
        b'"' => writer.write_all(b"\\\""),
        b'\\' => writer.write_all(b"\\\\"),
        0x08 => writer.write_all(b"\\b"),
        0x09 => writer.write_all(b"\\t"),
        0x0A => writer.write_all(b"\\n"),
        0x0C => writer.write_all(b"\\f"),
        0x0D => writer.write_all(b"\\r"),
        0x20..=0x7E => writer.write_byte(byte),
        _ => write_unicode_escape(writer, u32::from(byte)),
    }
}

fn write_json_codepoint(writer: &mut Writer<'_>, codepoint: u32) -> Result<(), TypeError> {
    if codepoint < 0x80 {
        return write_json_ascii(writer, codepoint as u8);
    }
    if codepoint < 0x10000 {
        return write_unicode_escape(writer, codepoint);
    }
    let offset = codepoint - 0x10000;
    write_unicode_escape(writer, 0xD800 + (offset >> 10))?;
    write_unicode_escape(writer, 0xDC00 + (offset & 0x3FF))
}

fn write_unicode_escape(writer: &mut Writer<'_>, value: u32) -> Result<(), TypeError> {
    writer.write_all(b"\\u")?;
    for shift in [12u32, 8, 4, 0] {
        let nibble = (value >> shift) & 0xF;
        let digit = if nibble < 10 {
            b'0' + nibble as u8
        } else {
            b'a' + (nibble as u8 - 10)
        };
        writer.write_byte(digit)?;
    }
    Ok(())
}

fn utf8_sequence_len(first: u8) -> usize {
    if first < 0x80 {
        1
    } else if first < 0xE0 {
        2
    } else if first < 0xF0 {
        3
    } else {
        4
    }
}

fn decode_utf8(bytes: &[u8], start: usize) -> Result<u32, TypeError> {
    let first = bytes[start];
    let length = utf8_sequence_len(first);
    if start + length > bytes.len() {
        return Err(TypeError::InvalidArgument);
    }
    // Reject overlong encodings, surrogate halves, and values above U+10FFFF
    // so every accepted byte sequence decodes to a single scalar value.
    match length {
        2 => {
            if first < 0xC2 {
                return Err(TypeError::InvalidArgument);
            }
        }
        3 => {
            if first == 0xE0 && bytes[start + 1] < 0xA0 {
                return Err(TypeError::InvalidArgument);
            }
            if first == 0xED && bytes[start + 1] >= 0xA0 {
                return Err(TypeError::InvalidArgument);
            }
        }
        4 => {
            if first == 0xF0 && bytes[start + 1] < 0x90 {
                return Err(TypeError::InvalidArgument);
            }
            if first == 0xF4 && bytes[start + 1] >= 0x90 {
                return Err(TypeError::InvalidArgument);
            }
            if first > 0xF4 {
                return Err(TypeError::InvalidArgument);
            }
        }
        _ => return Err(TypeError::InvalidArgument),
    }
    if !bytes[start + 1..start + length]
        .iter()
        .all(|&byte| byte & 0xC0 == 0x80)
    {
        return Err(TypeError::InvalidArgument);
    }
    let value = match length {
        2 => (u32::from(first & 0x1F) << 6) | u32::from(bytes[start + 1] & 0x3F),
        3 => {
            (u32::from(first & 0x0F) << 12)
                | (u32::from(bytes[start + 1] & 0x3F) << 6)
                | u32::from(bytes[start + 2] & 0x3F)
        }
        4 => {
            (u32::from(first & 0x07) << 18)
                | (u32::from(bytes[start + 1] & 0x3F) << 12)
                | (u32::from(bytes[start + 2] & 0x3F) << 6)
                | u32::from(bytes[start + 3] & 0x3F)
        }
        _ => return Err(TypeError::InvalidArgument),
    };
    Ok(value)
}

#[cfg(test)]
#[path = "../tests/unit/types.rs"]
mod tests;
