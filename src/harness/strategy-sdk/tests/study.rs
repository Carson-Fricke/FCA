//! The study authoring contract: bars only, structural capability layout.
//!
//! A study has no order surface by construction: its vtable carries exactly
//! `create`, `on_bar`, `on_stop`, and `destroy`, and there is no tick
//! callback to reach. The shared ABI version pins the hard cutover: every
//! pre-bar study artifact is rejected by the loader's version check.

use std::mem::{offset_of, size_of};

use harness_strategy_sdk::export::bar_view;
use harness_strategy_sdk::{CBarEvent, CDecimal, CStr, StudyProgramVTable, SDK_ABI_VERSION};

#[test]
fn the_study_vtable_carries_create_bar_stop_destroy_and_no_order_surface() {
    // The capability contract is structural: the vtable is a plain C struct
    // with the ABI header and exactly `create`, `on_bar`, `on_stop`,
    // `destroy`. There is no order callback and no tick callback, so a study
    // program has no order surface and folds bars only by construction.
    assert_eq!(offset_of!(StudyProgramVTable, abi_version), 0);
    assert_eq!(offset_of!(StudyProgramVTable, reserved), 4);
    assert_eq!(offset_of!(StudyProgramVTable, create), 8);
    assert_eq!(
        offset_of!(StudyProgramVTable, on_bar),
        8 + size_of::<usize>()
    );
    assert_eq!(
        offset_of!(StudyProgramVTable, on_stop),
        8 + 2 * size_of::<usize>()
    );
    assert_eq!(
        offset_of!(StudyProgramVTable, destroy),
        8 + 3 * size_of::<usize>()
    );
    assert_eq!(size_of::<StudyProgramVTable>(), 8 + 4 * size_of::<usize>());
    assert_eq!(SDK_ABI_VERSION, 2, "the bar cutover bumps the shared ABI");
}

#[test]
fn the_bar_view_preserves_exact_decimals_and_borrowed_strings() {
    let event_id = "bar-1";
    let symbol = "SPY";
    let raw = CBarEvent {
        event_id: CStr::from_slice(event_id),
        timestamp_ns: 1_704_206_400_000_000_000,
        sequence: 42,
        symbol: CStr::from_slice(symbol),
        open: decimal("450.00"),
        high: decimal("451.250"),
        low: decimal("449.750"),
        close: decimal("450.01"),
        volume: decimal("12345.00"),
    };
    let view = unsafe { bar_view(&raw) }.expect("bar view");
    assert_eq!(view.event_id, event_id);
    assert_eq!(view.timestamp_ns, 1_704_206_400_000_000_000);
    assert_eq!(view.sequence, 42);
    assert_eq!(view.symbol, symbol);
    // Exact crossing: scale and trailing zeros survive, so no float roundtrip.
    assert_eq!(view.open.to_string(), "450.00");
    assert_eq!(view.high.to_string(), "451.250");
    assert_eq!(view.low.to_string(), "449.750");
    assert_eq!(view.close.to_string(), "450.01");
    assert_eq!(view.volume.to_string(), "12345.00");
}

fn decimal(text: &str) -> CDecimal {
    CDecimal::from_decimal(text.parse().expect("decimal literal"))
}
