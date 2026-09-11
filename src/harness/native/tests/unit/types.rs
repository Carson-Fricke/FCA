use super::{
    canonical_symbol, decimal_text, deterministic_id, isoformat_utc, serialize_trade_row,
    SymbolKind, TradeRow, MAX_UNSCALED,
};

#[test]
fn canonical_symbols_match_the_python_canonicalization() {
    let mut buffer = [0u8; 256];

    let (len, kind) = canonical_symbol(b" aapl ", &mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"AAPL");
    assert_eq!(kind, SymbolKind::Market);

    let (len, _) = canonical_symbol(b"BRK.B", &mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"BRK.B");

    let (len, kind) = canonical_symbol(b"AAPL240119C00150000", &mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"AAPL240119C00150000");
    assert_eq!(kind, SymbolKind::Osi);

    let (len, kind) = canonical_symbol(b"AAPL  240119C00150000", &mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"AAPL240119C00150000");
    assert_eq!(kind, SymbolKind::Osi);

    assert!(canonical_symbol(b"../AAPL", &mut buffer).is_err());
    assert!(canonical_symbol(b"   ", &mut buffer).is_err());
    assert!(canonical_symbol(b"AAPL", &mut buffer[..3]).is_err());
}

#[test]
fn deterministic_ids_match_the_python_derivation() {
    let cases: [(&[u8], &[u8], u64, &str); 6] = [
        (b"str:abc", b"engine", 0, "5e07f8554e5454c59b2e82a1c23e8715"),
        (b"str:abc", b"event", 1, "5f4bc7e603ae59d7b9c9d8629073e430"),
        (b"str:abc", b"order", 7, "ca17e7b50d2b5a1589208240bda7da65"),
        (
            b"str:abc",
            b"fill",
            123456789012345,
            "0ee8754800535aac8f01b962ae39ef7d",
        ),
        (b"int:42", b"engine", 0, "13a861e80e2c570cade52b8498edf905"),
        (b"XY", b"engine", 3, "617fa2b4386d5472b63a760a3bae48bb"),
    ];
    for (seed, kind, ordinal, hex) in cases {
        let mut id = [0u8; 16];
        deterministic_id(seed, kind, ordinal, &mut id).unwrap();
        let expected: Vec<u8> = (0..16)
            .map(|index| u8::from_str_radix(&hex[index * 2..index * 2 + 2], 16).unwrap())
            .collect();
        assert_eq!(&id[..], &expected[..]);
    }
    let mut sink = [0u8; 16];
    assert!(deterministic_id(b"", b"engine", 0, &mut sink).is_err());
    assert!(deterministic_id(b"seed", b"", 0, &mut sink).is_err());
}

#[test]
fn decimal_text_matches_python_str_decimal() {
    let cases: [(i128, &str); 11] = [
        (1_500_000_000_000_000_000, "1.500000000000000000"),
        (1, "1E-18"),
        (0, "0E-18"),
        (-1, "-1E-18"),
        (99_999_999_999_999_999_999, "99.999999999999999999"),
        (-1_502_500_000_000_000_000, "-1.502500000000000000"),
        (12_345_678_901_234_567_890, "12.345678901234567890"),
        (-99_999_999_999_999_999_999, "-99.999999999999999999"),
        (15_025, "1.5025E-14"),
        (1_000_000_000_000_000_000, "1.000000000000000000"),
        (
            1_208_925_819_614_629_174_706_176,
            "1208925.819614629174706176",
        ),
    ];
    let mut buffer = [0u8; 64];
    for (unscaled, text) in cases {
        let len = decimal_text(unscaled, 18, &mut buffer).unwrap();
        assert_eq!(&buffer[..len], text.as_bytes());
    }
    assert!(decimal_text(MAX_UNSCALED as i128 + 1, 18, &mut buffer).is_err());
}

#[test]
fn isoformat_matches_python_datetime() {
    let mut buffer = [0u8; 40];
    let len = isoformat_utc(1_704_164_645_123_456_000, &mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"2024-01-02T03:04:05.123456+00:00");
    let len = isoformat_utc(1_704_164_645_000_000_000, &mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"2024-01-02T03:04:05+00:00");
    assert!(isoformat_utc(1_704_164_645_000_000_001, &mut buffer).is_err());
}

#[test]
fn trade_rows_serialize_byte_identically() {
    let first = "{\"event_id\":\"evt-1\",\"timestamp\":\"2024-01-02T03:04:05.123456+00:00\",\"sequence\":9,\"symbol\":\"AAPL\",\"price\":\"150.250000000000000000\",\"quantity\":\"10.000000000000000000\",\"venue\":\"XNAS\",\"trade_id\":\"t-7\"}";
    let second = "{\"event_id\":\"evt-2\",\"timestamp\":\"2024-01-02T03:04:05.123456+00:00\",\"sequence\":0,\"symbol\":\"AAPL240119C00150000\",\"price\":\"1E-18\",\"quantity\":\"99.999999999999999999\",\"venue\":null,\"trade_id\":null}";
    let third = "{\"event_id\":\"evt-3\",\"timestamp\":\"2024-01-02T03:04:05+00:00\",\"sequence\":0,\"symbol\":\"BRK.B\",\"price\":\"150.250000000000000000\",\"quantity\":\"10.000000000000000000\",\"venue\":\"XNAS\",\"trade_id\":null}";

    let mut buffer = [0u8; 512];
    let len = serialize_trade_row(
        &TradeRow {
            event_id: "evt-1",
            timestamp_ns: 1_704_164_645_123_456_000,
            sequence: 9,
            symbol: "AAPL",
            price_unscaled: 150_250_000_000_000_000_000,
            quantity_unscaled: 10_000_000_000_000_000_000,
            venue: Some("XNAS"),
            trade_id: Some("t-7"),
        },
        &mut buffer,
    )
    .unwrap();
    assert_eq!(&buffer[..len], first.as_bytes());

    let len = serialize_trade_row(
        &TradeRow {
            event_id: "evt-2",
            timestamp_ns: 1_704_164_645_123_456_000,
            sequence: 0,
            symbol: "AAPL240119C00150000",
            price_unscaled: 1,
            quantity_unscaled: 99_999_999_999_999_999_999,
            venue: None,
            trade_id: None,
        },
        &mut buffer,
    )
    .unwrap();
    assert_eq!(&buffer[..len], second.as_bytes());

    let len = serialize_trade_row(
        &TradeRow {
            event_id: "evt-3",
            timestamp_ns: 1_704_164_645_000_000_000,
            sequence: 0,
            symbol: "BRK.B",
            price_unscaled: 150_250_000_000_000_000_000,
            quantity_unscaled: 10_000_000_000_000_000_000,
            venue: Some("XNAS"),
            trade_id: None,
        },
        &mut buffer,
    )
    .unwrap();
    assert_eq!(&buffer[..len], third.as_bytes());

    assert!(serialize_trade_row(
        &TradeRow {
            event_id: "evt-4",
            timestamp_ns: 0,
            sequence: 0,
            symbol: "aapl",
            price_unscaled: 0,
            quantity_unscaled: 0,
            venue: None,
            trade_id: None,
        },
        &mut buffer,
    )
    .is_err());
}

#[test]
fn json_strings_escape_like_python() {
    let cases: [(&str, &str); 3] = [
        ("plain", "\"plain\""),
        (
            "a\"b\\c\nd\u{01}\u{7f}",
            "\"a\\\"b\\\\c\\nd\\u0001\\u007f\"",
        ),
        (
            "\u{00e9}\u{2028}\u{1f600}",
            "\"\\u00e9\\u2028\\ud83d\\ude00\"",
        ),
    ];
    for (value, expected) in cases {
        let mut buffer = [0u8; 128];
        let mut writer = super::Writer::new(&mut buffer);
        super::write_json_string(&mut writer, value.as_bytes()).unwrap();
        let written = writer.written();
        assert_eq!(&buffer[..written], expected.as_bytes());
    }
}
