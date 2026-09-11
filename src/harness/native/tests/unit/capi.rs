use super::*;

#[test]
fn abi_export_matches_the_pinned_version() {
    assert_eq!(native_abi_version(), ABI_VERSION);
}

#[test]
fn exports_report_error_codes_instead_of_panicking() {
    let mut buffer = [0u8; 64];
    let mut written = 0usize;
    let ok = unsafe {
        harness_symbol_canonicalize(
            b" aapl ".as_ptr(),
            6,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut written,
        )
    };
    assert_eq!(ok, code::OK);
    assert_eq!(written, 4);
    let bad = unsafe {
        harness_symbol_canonicalize(
            core::ptr::null(),
            4,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut written,
        )
    };
    assert_eq!(bad, code::INVALID_ARGUMENT);
    let symbol = unsafe {
        harness_symbol_canonicalize(
            b"../AAPL".as_ptr(),
            7,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut written,
        )
    };
    assert_eq!(symbol, code::INVALID_SYMBOL);

    let mut uuid = [0u8; 16];
    let id =
        unsafe { harness_id_derive(core::ptr::null(), 0, b"k".as_ptr(), 1, 0, uuid.as_mut_ptr()) };
    assert_eq!(id, code::INVALID_ARGUMENT);

    let packed = 150_250_000_000_000_000_000i128.to_le_bytes();
    let mut text_len = 0usize;
    let decimal = unsafe {
        harness_decimal_text(
            packed.as_ptr(),
            18,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut text_len,
        )
    };
    assert_eq!(decimal, code::OK);
    assert_eq!(&buffer[..text_len], b"150.250000000000000000");
}
