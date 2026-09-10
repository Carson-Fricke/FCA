//! Stable C ABI surface of the native harness core.
//!
//! Every export returns a small integer error code and writes results through
//! caller-provided buffers, so no allocation happens across the boundary and no
//! panic can escape. Variable-length inputs are passed as pointer + length
//! pairs (not NUL-terminated), and 128-bit values cross the boundary as 16
//! little-endian bytes because `__int128` is not portable across C ABIs.

use core::ffi::{c_char, c_int};
use core::slice;

use crate::types::{
    canonical_symbol, decimal_text, deterministic_id, serialize_trade_row, TypeError, TradeRow,
    ABI_VERSION,
};

/// Error codes returned by every export.
pub mod code {
    /// Success.
    pub const OK: i32 = 0;
    /// A required argument was invalid.
    pub const INVALID_ARGUMENT: i32 = 1;
    /// The symbol was invalid.
    pub const INVALID_SYMBOL: i32 = 2;
    /// The output buffer was too small.
    pub const BUFFER_TOO_SMALL: i32 = 3;
    /// The decimal value or scale was invalid.
    pub const INVALID_DECIMAL: i32 = 4;
    /// The timestamp was invalid.
    pub const INVALID_TIMESTAMP: i32 = 5;
    /// The value was out of range.
    pub const OUT_OF_RANGE: i32 = 6;
    /// An unexpected failure occurred.
    pub const UNKNOWN: i32 = 7;
}

fn error_code(error: TypeError) -> c_int {
    match error {
        TypeError::InvalidArgument => code::INVALID_ARGUMENT,
        TypeError::InvalidSymbol => code::INVALID_SYMBOL,
        TypeError::BufferTooSmall => code::BUFFER_TOO_SMALL,
        TypeError::InvalidDecimal => code::INVALID_DECIMAL,
        TypeError::InvalidTimestamp => code::INVALID_TIMESTAMP,
        TypeError::OutOfRange => code::OUT_OF_RANGE,
    }
}

/// Semantic version of this crate as a NUL-terminated C string.
static VERSION: &[u8] = b"0.1.0\0";

/// Returns a pointer to the crate's NUL-terminated version string.
///
/// The returned pointer references static storage owned by this library;
/// callers must not free or mutate it.
#[no_mangle]
pub extern "C" fn harness_native_version() -> *const c_char {
    VERSION.as_ptr().cast::<c_char>()
}

/// Version of the stable C ABI. The Python loader pins this value.
#[no_mangle]
pub extern "C" fn harness_native_abi_version() -> u32 {
    ABI_VERSION
}

/// # Safety
///
/// `ptr` must be null when `len == 0`, or point to at least `len` readable
/// bytes.
unsafe fn slice_from<'a>(ptr: *const u8, len: usize) -> Result<&'a [u8], c_int> {
    if len == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(code::INVALID_ARGUMENT);
    }
    Ok(unsafe { slice::from_raw_parts(ptr, len) })
}

/// # Safety
///
/// `ptr` must point to at least `cap` writable bytes.
unsafe fn out_slice<'a>(ptr: *mut u8, cap: usize) -> Result<&'a mut [u8], c_int> {
    if ptr.is_null() {
        return Err(code::INVALID_ARGUMENT);
    }
    Ok(unsafe { slice::from_raw_parts_mut(ptr, cap) })
}

/// Canonicalize a market or OSI symbol, writing the canonical bytes to
/// `out_ptr` and the length to `out_len`.
///
/// # Safety
///
/// The input pointer/length pair and output pointer/capacity must describe
/// valid buffers, and `out_len` must be a valid writable pointer.
#[no_mangle]
pub unsafe extern "C" fn harness_symbol_canonicalize(
    in_ptr: *const u8,
    in_len: usize,
    out_ptr: *mut u8,
    out_cap: usize,
    out_len: *mut usize,
) -> c_int {
    let input = match unsafe { slice_from(in_ptr, in_len) } {
        Ok(input) => input,
        Err(code) => return code,
    };
    let out = match unsafe { out_slice(out_ptr, out_cap) } {
        Ok(out) => out,
        Err(code) => return code,
    };
    if out_len.is_null() {
        return code::INVALID_ARGUMENT;
    }
    match canonical_symbol(input, out) {
        Ok((len, _)) => {
            unsafe { *out_len = len };
            code::OK
        }
        Err(error) => error_code(error),
    }
}

/// Derive a deterministic UUID, writing 16 raw bytes to `out_uuid`.
///
/// # Safety
///
/// The input pointers must describe valid buffers of their lengths, and
/// `out_uuid` must point to at least 16 writable bytes.
#[no_mangle]
pub unsafe extern "C" fn harness_id_derive(
    seed_ptr: *const u8,
    seed_len: usize,
    kind_ptr: *const u8,
    kind_len: usize,
    ordinal: u64,
    out_uuid: *mut u8,
) -> c_int {
    let seed = match unsafe { slice_from(seed_ptr, seed_len) } {
        Ok(seed) => seed,
        Err(code) => return code,
    };
    let kind = match unsafe { slice_from(kind_ptr, kind_len) } {
        Ok(kind) => kind,
        Err(code) => return code,
    };
    if out_uuid.is_null() {
        return code::INVALID_ARGUMENT;
    }
    let out = unsafe { slice::from_raw_parts_mut(out_uuid, 16) };
    let mut uuid = [0u8; 16];
    match deterministic_id(seed, kind, ordinal, &mut uuid) {
        Ok(()) => {
            out.copy_from_slice(&uuid);
            code::OK
        }
        Err(error) => error_code(error),
    }
}

/// Render an unscaled 128-bit integer carried at `scale` as the canonical
/// decimal text.
///
/// # Safety
///
/// `unscaled_le` must point to at least 16 readable bytes; the output
/// pointer/capacity must describe a valid buffer; `out_len` must be writable.
#[no_mangle]
pub unsafe extern "C" fn harness_decimal_text(
    unscaled_le: *const u8,
    scale: u32,
    out_ptr: *mut u8,
    out_cap: usize,
    out_len: *mut usize,
) -> c_int {
    if unscaled_le.is_null() {
        return code::INVALID_ARGUMENT;
    }
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(unsafe { slice::from_raw_parts(unscaled_le, 16) });
    let unscaled = i128::from_le_bytes(bytes);
    let out = match unsafe { out_slice(out_ptr, out_cap) } {
        Ok(out) => out,
        Err(code) => return code,
    };
    if out_len.is_null() {
        return code::INVALID_ARGUMENT;
    }
    match decimal_text(unscaled, scale, out) {
        Ok(len) => {
            unsafe { *out_len = len };
            code::OK
        }
        Err(error) => error_code(error),
    }
}

/// Serialize a canonical trade row to canonical JSON bytes. Optional strings
/// are passed as null pointers.
///
/// # Safety
///
/// Every non-null pointer must describe a valid buffer of its paired length,
/// and the output pointer/capacity must describe a valid buffer.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn harness_trade_row_serialize(
    event_id_ptr: *const u8,
    event_id_len: usize,
    timestamp_ns_le: *const u8,
    sequence: u64,
    symbol_ptr: *const u8,
    symbol_len: usize,
    price_unscaled_le: *const u8,
    quantity_unscaled_le: *const u8,
    venue_ptr: *const u8,
    venue_len: usize,
    trade_id_ptr: *const u8,
    trade_id_len: usize,
    out_ptr: *mut u8,
    out_cap: usize,
    out_len: *mut usize,
) -> c_int {
    if timestamp_ns_le.is_null() || price_unscaled_le.is_null() || quantity_unscaled_le.is_null() {
        return code::INVALID_ARGUMENT;
    }
    let event_id = match unsafe { slice_from(event_id_ptr, event_id_len) } {
        Ok(value) => value,
        Err(code) => return code,
    };
    let symbol = match unsafe { slice_from(symbol_ptr, symbol_len) } {
        Ok(value) => value,
        Err(code) => return code,
    };
    let venue = if venue_ptr.is_null() {
        Ok(None)
    } else {
        unsafe { slice_from(venue_ptr, venue_len) }.map(Some)
    };
    let venue = match venue {
        Ok(value) => value,
        Err(code) => return code,
    };
    let trade_id = if trade_id_ptr.is_null() {
        Ok(None)
    } else {
        unsafe { slice_from(trade_id_ptr, trade_id_len) }.map(Some)
    };
    let trade_id = match trade_id {
        Ok(value) => value,
        Err(code) => return code,
    };
    let out = match unsafe { out_slice(out_ptr, out_cap) } {
        Ok(out) => out,
        Err(code) => return code,
    };
    if out_len.is_null() {
        return code::INVALID_ARGUMENT;
    }

    let event_id = match core::str::from_utf8(event_id) {
        Ok(value) => value,
        Err(_) => return code::INVALID_ARGUMENT,
    };
    let symbol = match core::str::from_utf8(symbol) {
        Ok(value) => value,
        Err(_) => return code::INVALID_ARGUMENT,
    };
    let venue = match venue.map(core::str::from_utf8).transpose() {
        Ok(value) => value,
        Err(_) => return code::INVALID_ARGUMENT,
    };
    let trade_id = match trade_id.map(core::str::from_utf8).transpose() {
        Ok(value) => value,
        Err(_) => return code::INVALID_ARGUMENT,
    };

    let mut timestamp_bytes = [0u8; 16];
    timestamp_bytes.copy_from_slice(unsafe { slice::from_raw_parts(timestamp_ns_le, 16) });
    let mut price_bytes = [0u8; 16];
    price_bytes.copy_from_slice(unsafe { slice::from_raw_parts(price_unscaled_le, 16) });
    let mut quantity_bytes = [0u8; 16];
    quantity_bytes.copy_from_slice(unsafe { slice::from_raw_parts(quantity_unscaled_le, 16) });

    let row = TradeRow {
        event_id,
        timestamp_ns: i128::from_le_bytes(timestamp_bytes),
        sequence,
        symbol,
        price_unscaled: i128::from_le_bytes(price_bytes),
        quantity_unscaled: i128::from_le_bytes(quantity_bytes),
        venue,
        trade_id,
    };
    match serialize_trade_row(&row, out) {
        Ok(len) => {
            unsafe { *out_len = len };
            code::OK
        }
        Err(error) => error_code(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn abi_export_matches_the_pinned_version() {
        assert_eq!(harness_native_abi_version(), ABI_VERSION);
    }

    #[test]
    fn exports_report_error_codes_instead_of_panicking() {
        let mut buffer = [0u8; 64];
        let mut written = 0usize;
        let ok = unsafe {
            harness_symbol_canonicalize(b" aapl ".as_ptr(), 6, buffer.as_mut_ptr(), buffer.len(), &mut written)
        };
        assert_eq!(ok, code::OK);
        assert_eq!(written, 4);
        let bad = unsafe {
            harness_symbol_canonicalize(core::ptr::null(), 4, buffer.as_mut_ptr(), buffer.len(), &mut written)
        };
        assert_eq!(bad, code::INVALID_ARGUMENT);
        let symbol = unsafe {
            harness_symbol_canonicalize(b"../AAPL".as_ptr(), 7, buffer.as_mut_ptr(), buffer.len(), &mut written)
        };
        assert_eq!(symbol, code::INVALID_SYMBOL);

        let mut uuid = [0u8; 16];
        let id = unsafe { harness_id_derive(core::ptr::null(), 0, b"k".as_ptr(), 1, 0, uuid.as_mut_ptr()) };
        assert_eq!(id, code::INVALID_ARGUMENT);

        let packed = 150_250_000_000_000_000_000i128.to_le_bytes();
        let mut text_len = 0usize;
        let decimal = unsafe {
            harness_decimal_text(packed.as_ptr(), 18, buffer.as_mut_ptr(), buffer.len(), &mut text_len)
        };
        assert_eq!(decimal, code::OK);
        assert_eq!(&buffer[..text_len], b"150.250000000000000000");
    }
}
