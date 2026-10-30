//! Exported callback behavior and the steady-state routing allocation budget.
use harness_strategy_sdk::{
    export, CDecimal, CQuoteEvent, CStr, Indicator, IndicatorSequence, MarketConsumer,
    ObservationContext, QuoteEvent, StudyProgram, Subscription, WaveletIndicator, SDK_ERROR,
    SDK_OK,
};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

thread_local! {
    static COUNTING: Cell<bool> = const { Cell::new(false) };
    static ALLOCATIONS: Cell<usize> = const { Cell::new(0) };
    static CALLBACKS: Cell<usize> = const { Cell::new(0) };
}

struct Counted;
#[global_allocator]
static ALLOCATOR: Counted = Counted;

fn allocation() {
    let _ = COUNTING.try_with(|counting| {
        if counting.get() {
            ALLOCATIONS.with(|count| count.set(count.get() + 1));
        }
    });
}

unsafe impl GlobalAlloc for Counted {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        allocation();
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { System.dealloc(pointer, layout) }
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        allocation();
        unsafe { System.realloc(pointer, layout, size) }
    }
}

struct Program {
    declarations: Vec<Subscription>,
    wavelet: WaveletIndicator,
    replacement: WaveletIndicator,
    mode: String,
    replaced: bool,
}

impl StudyProgram for Program {
    fn new(parameters: harness_strategy_sdk::serde_json::Value) -> Self {
        let wavelet = || WaveletIndicator::new(vec!["MSFT".into()], "1d".into(), 32, 8).unwrap();
        Self {
            declarations: vec![
                Subscription::Bars {
                    symbols: vec!["MSFT".into()],
                    interval: "5min".into(),
                },
                Subscription::Bars {
                    symbols: vec!["MSFT".into()],
                    interval: "1d".into(),
                },
                Subscription::Quote {
                    symbols: vec!["MSFT".into()],
                },
            ],
            wavelet: wavelet(),
            replacement: wavelet(),
            mode: parameters["mode"].as_str().unwrap_or("").into(),
            replaced: false,
        }
    }
}

impl MarketConsumer for Program {
    type Context = ObservationContext;

    fn subscriptions(&self) -> Vec<Subscription> {
        self.declarations.clone()
    }

    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        if self.replaced {
            vec![&mut self.replacement]
        } else {
            vec![&mut self.wavelet]
        }
    }

    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        CALLBACKS.with(|calls| calls.set(calls.get() + 1));
        match self.mode.as_str() {
            "replace" => self.replaced = true,
            "cadence" => *self.wavelet.sequence_mut() = IndicatorSequence::ticks(10).unwrap(),
            "declarations" => self.declarations.clear(),
            "permutation" => self.declarations.reverse(),
            _ => {}
        }
    }
}

fn quote() -> CQuoteEvent {
    CQuoteEvent {
        event_id: CStr::from_slice("quote"),
        timestamp_ns: 1,
        sequence: 1,
        symbol: CStr::from_slice("MSFT"),
        bid: CDecimal::from_decimal(100.into()),
        ask: CDecimal::from_decimal(101.into()),
        bid_quantity: CDecimal::from_decimal(1.into()),
        ask_quantity: CDecimal::from_decimal(1.into()),
        venue: CStr::from_slice("test"),
    }
}

#[test]
fn repeated_exported_quotes_reuse_plan_and_traversal_storage() {
    let state = unsafe { export::__study_create::<Program>(CStr::from_slice("{}")) };
    assert!(!state.is_null());
    let raw = quote();
    assert_eq!(
        unsafe { export::__study_on_quote::<Program>(state, &raw) },
        SDK_OK
    );
    ALLOCATIONS.with(|count| count.set(0));
    COUNTING.with(|counting| counting.set(true));
    let mut result = SDK_OK;
    for _ in 0..32 {
        result |= unsafe { export::__study_on_quote::<Program>(state, &raw) };
    }
    COUNTING.with(|counting| counting.set(false));
    let allocations = ALLOCATIONS.with(Cell::get);
    unsafe { export::__study_destroy::<Program>(state) };
    assert_eq!(result, SDK_OK);
    // This public-boundary budget catches the old 228-allocation graph rebuild.
    // Declaration Vecs still belong to the existing authoring trait contract.
    assert!(
        allocations <= 64 * 32,
        "{allocations} allocations for 32 quote callbacks"
    );
}

#[test]
fn exported_callbacks_reject_topology_cadence_and_declaration_mutation_and_stay_failed() {
    for mode in ["replace", "cadence", "declarations"] {
        let parameters = format!(r#"{{"mode":"{mode}"}}"#);
        let state = unsafe { export::__study_create::<Program>(CStr::from_slice(&parameters)) };
        assert!(!state.is_null());
        CALLBACKS.with(|calls| calls.set(0));
        let raw = quote();
        assert_eq!(
            unsafe { export::__study_on_quote::<Program>(state, &raw) },
            SDK_ERROR,
            "{mode}"
        );
        assert_eq!(
            unsafe { export::__study_on_quote::<Program>(state, &raw) },
            SDK_ERROR,
            "{mode}"
        );
        assert_eq!(CALLBACKS.with(Cell::get), 1, "{mode}");
        unsafe { export::__study_destroy::<Program>(state) };
    }
}

#[test]
fn semantically_equal_declaration_reordering_remains_valid_with_the_same_plan() {
    let state =
        unsafe { export::__study_create::<Program>(CStr::from_slice(r#"{"mode":"permutation"}"#)) };
    assert!(!state.is_null());
    let raw = quote();
    for _ in 0..4 {
        assert_eq!(
            unsafe { export::__study_on_quote::<Program>(state, &raw) },
            SDK_OK
        );
    }
    unsafe { export::__study_destroy::<Program>(state) };
}
