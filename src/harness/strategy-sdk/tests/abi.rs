//! The C ABI decimal crossing is lossless.
//!
//! Every engine decimal crosses the dylib boundary as a `CDecimal`; these
//! tests pin the exact roundtrip through the shapes that have actually
//! broken naive fixed-point ports: preserved trailing zeros, negative zero,
//! positive exponents, and full-width coefficients.

use harness_strategy_sdk::{
    export, BarFrame, CBarEvent, CBarFrame, CDecimal, CStr, Decimal, StudyProgram, SDK_ABI_VERSION,
    SDK_ERROR, SDK_OK,
};

fn roundtrip(text: &str) {
    let value: Decimal = text.parse().expect("decimal parses");
    let crossing = CDecimal::from_decimal(value);
    let restored = crossing.to_decimal();
    assert_eq!(
        restored.to_string(),
        value.to_string(),
        "display roundtrip for {text}"
    );
    assert_eq!(restored, value, "value roundtrip for {text}");
}

#[test]
fn decimal_roundtrip_preserves_scale_sign_and_zero() {
    roundtrip("1.500");
    roundtrip("450.01");
    roundtrip("0");
    roundtrip("-0");
    roundtrip("1E+2");
    roundtrip("-1E-8");
    roundtrip("100");
    roundtrip("0.000000001");
    roundtrip("-123.45678901234567890123456789012345678");
    roundtrip("340282366920938463463374607431768211455");
}

fn bar(symbol: &'static str) -> CBarEvent {
    CBarEvent {
        event_id: CStr::from_slice("bar"),
        timestamp_ns: 10,
        sequence: 1,
        symbol: CStr::from_slice(symbol),
        open: CDecimal::from_decimal("1.500".parse().unwrap()),
        high: CDecimal::from_decimal("340282366920938463463374607431768211455".parse().unwrap()),
        low: CDecimal::from_decimal("-0".parse().unwrap()),
        close: CDecimal::from_decimal("1E+2".parse().unwrap()),
        volume: CDecimal::from_decimal("0.000000001".parse().unwrap()),
    }
}

fn frame(bars: &[CBarEvent], missing: &[CStr]) -> CBarFrame {
    CBarFrame {
        interval_start_ns: 10,
        interval_end_ns: 20,
        available_at_ns: 25,
        bars: bars.as_ptr(),
        bars_len: bars.len(),
        missing_symbols: missing.as_ptr(),
        missing_symbols_len: missing.len(),
    }
}

#[test]
fn frame_reconstruction_is_atomic_ordered_and_decimal_exact() {
    assert_eq!(SDK_ABI_VERSION, 3);
    let bars = [bar("ZZZ"), bar("AAA")];
    let missing = [CStr::from_slice("BBB")];
    let raw = frame(&bars, &missing);
    let view = unsafe { export::bar_frame_view(&raw) }.expect("valid frame");
    assert_eq!(view.interval_start_ns, 10);
    assert_eq!(view.interval_end_ns, 20);
    assert_eq!(view.available_at_ns, 25);
    assert_eq!(
        view.bars_by_symbol.keys().copied().collect::<Vec<_>>(),
        ["AAA", "ZZZ"]
    );
    assert_eq!(view.missing_symbols, ["BBB"]);
    for event in view.bars_by_symbol.values() {
        assert_eq!(event.timestamp_ns, 10);
        assert_eq!(event.open.to_string(), "1.500");
        assert_eq!(
            event.high.to_string(),
            "340282366920938463463374607431768211455"
        );
        assert_eq!(event.low.to_string(), "-0");
        assert_eq!(event.close.to_string(), "1E+2");
        assert_eq!(event.volume.to_string(), "1E-9");
    }
}

#[test]
fn frame_rejects_invalid_bounds_timestamps_and_symbol_duplicates() {
    let bars = [bar("AAA"), bar("AAA")];
    let raw = frame(&bars, &[]);
    assert!(unsafe { export::bar_frame_view(&raw) }.is_none());

    let bars = [bar("AAA")];
    let raw = frame(&bars, &[]);
    for invalid in [
        CBarFrame {
            interval_end_ns: 10,
            ..raw
        },
        CBarFrame {
            interval_end_ns: 9,
            ..raw
        },
        CBarFrame {
            available_at_ns: 19,
            ..raw
        },
        CBarFrame {
            interval_start_ns: 11,
            ..raw
        },
    ] {
        assert!(unsafe { export::bar_frame_view(&invalid) }.is_none());
    }
    for missing in [
        vec![CStr::from_slice("AAA")],
        vec![CStr::from_slice("BBB"), CStr::from_slice("BBB")],
    ] {
        assert!(unsafe { export::bar_frame_view(&frame(&bars, &missing)) }.is_none());
    }
    let at_end = CBarFrame {
        available_at_ns: 20,
        ..raw
    };
    assert!(unsafe { export::bar_frame_view(&at_end) }.is_some());
}

#[test]
fn frame_checks_null_alignment_and_pointer_count_bounds_before_dereferencing() {
    let raw = frame(&[], &[]);
    let empty = CBarFrame {
        bars: std::ptr::null(),
        missing_symbols: std::ptr::null(),
        ..raw
    };
    assert!(unsafe { export::bar_frame_view(&empty) }.is_some());
    assert!(unsafe { export::bar_frame_view(std::ptr::null()) }.is_none());
    let unaligned_frame = std::ptr::NonNull::<CBarFrame>::dangling()
        .as_ptr()
        .cast::<u8>()
        .wrapping_add(1)
        .cast::<CBarFrame>();
    assert!(unsafe { export::bar_frame_view(unaligned_frame) }.is_none());
    for invalid in [
        CBarFrame {
            bars_len: 1,
            ..empty
        },
        CBarFrame {
            missing_symbols_len: 1,
            ..empty
        },
        CBarFrame {
            bars_len: usize::MAX,
            ..raw
        },
        CBarFrame {
            missing_symbols_len: usize::MAX,
            ..raw
        },
        CBarFrame {
            bars: std::ptr::dangling::<CBarEvent>()
                .cast::<u8>()
                .wrapping_add(1)
                .cast(),
            bars_len: 1,
            ..raw
        },
        CBarFrame {
            missing_symbols: std::ptr::dangling::<CStr>()
                .cast::<u8>()
                .wrapping_add(1)
                .cast(),
            missing_symbols_len: 1,
            ..raw
        },
    ] {
        assert!(unsafe { export::bar_frame_view(&invalid) }.is_none());
    }
    let invalid_strings = [
        CStr {
            ptr: std::ptr::null(),
            len: 0,
        },
        CStr {
            ptr: b"A".as_ptr(),
            len: usize::MAX,
        },
        CStr {
            ptr: [0xff].as_ptr(),
            len: 1,
        },
    ];
    for string in invalid_strings {
        let bars = [CBarEvent {
            symbol: string,
            ..bar("AAA")
        }];
        assert!(unsafe { export::bar_frame_view(&frame(&bars, &[])) }.is_none());
        assert!(unsafe { export::bar_frame_view(&frame(&[], &[string])) }.is_none());
    }
}

#[derive(Default)]
struct FrameObserver {
    callbacks: usize,
    panic: bool,
}

impl StudyProgram for FrameObserver {
    fn new(_: harness_strategy_sdk::serde_json::Value) -> Self {
        Self::default()
    }

    fn on_bar_frame(&mut self, frame: &BarFrame) {
        self.callbacks += 1;
        assert_eq!(frame.bars_by_symbol.len(), 2);
        assert!(!self.panic, "frame author panic");
    }
}

#[test]
fn frame_trampoline_calls_author_once_rejects_invalid_input_and_contains_panics() {
    let mut observer = FrameObserver::default();
    let state = (&mut observer as *mut FrameObserver).cast();
    let bars = [bar("AAA"), bar("BBB")];
    let raw = frame(&bars, &[]);
    assert_eq!(
        unsafe { export::__study_on_bar_frame::<FrameObserver>(state, &raw) },
        SDK_OK
    );
    assert_eq!(observer.callbacks, 1);
    let invalid = CBarFrame {
        available_at_ns: 0,
        ..raw
    };
    assert_eq!(
        unsafe { export::__study_on_bar_frame::<FrameObserver>(state, &invalid) },
        SDK_ERROR
    );
    assert_eq!(observer.callbacks, 1);
    assert_eq!(
        unsafe { export::__study_on_bar_frame::<FrameObserver>(std::ptr::null_mut(), &raw) },
        SDK_ERROR
    );
    observer.panic = true;
    assert_eq!(
        unsafe { export::__study_on_bar_frame::<FrameObserver>(state, &raw) },
        SDK_ERROR
    );
    assert_eq!(observer.callbacks, 2);
    let error = unsafe { std::ffi::CStr::from_ptr(export::last_error_ptr()) }
        .to_str()
        .unwrap();
    assert!(error.contains("frame author panic"));
}
