//! Proves the hot arithmetic paths perform no heap allocation.
//!
//! A thread-local flag gates a counting global allocator, so only allocations
//! made by this test's own thread are counted. The test binaries run one test,
//! so the measurement is not polluted by parallel tests.

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::hint::black_box;
use std::sync::atomic::{AtomicUsize, Ordering};

use num::{Decimal18, RoundingMode};

thread_local! {
    static MEASURING: Cell<bool> = const { Cell::new(false) };
}

static ALLOCATIONS: AtomicUsize = AtomicUsize::new(0);

struct CountingAllocator;

unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if MEASURING.with(|flag| flag.get()) {
            ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
        }
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if MEASURING.with(|flag| flag.get()) {
            ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
        }
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static ALLOCATOR: CountingAllocator = CountingAllocator;

#[test]
fn hot_arithmetic_paths_do_not_allocate() {
    let a = Decimal18::from_unscaled(123_456_789_012_345_678).unwrap();
    let b = Decimal18::from_unscaled(-987_654_321_098_765).unwrap();
    let one = Decimal18::ONE;

    // Warm up any lazy state and exercise the operators before measuring.
    let _ = black_box(a.checked_mul(b));
    let _ = black_box(a + b);

    ALLOCATIONS.store(0, Ordering::Relaxed);
    MEASURING.with(|flag| flag.set(true));
    let mut accumulator = Decimal18::ZERO;
    for _ in 0..10_000 {
        accumulator = black_box(accumulator.checked_add(a).unwrap());
        accumulator = black_box(accumulator.checked_sub(b).unwrap());
        accumulator = black_box(accumulator.checked_mul(one).unwrap());
        accumulator = black_box(accumulator.checked_div(one).unwrap());
        accumulator = black_box(accumulator.checked_mul_div(a, one).unwrap());
        accumulator = black_box(
            accumulator
                .checked_div_with(one, RoundingMode::Floor)
                .unwrap(),
        );
        accumulator = black_box(accumulator.round_to(RoundingMode::HalfEven, 6).unwrap());
        let _ = black_box(accumulator.to_i128(RoundingMode::HalfEven).unwrap());
        let _ = black_box(accumulator.cmp(&b));
        let _ = black_box(accumulator.unscaled());
    }
    MEASURING.with(|flag| flag.set(false));

    let allocations = ALLOCATIONS.load(Ordering::Relaxed);
    assert_eq!(
        allocations, 0,
        "hot arithmetic allocated {allocations} times"
    );
    black_box(accumulator);
}
