//! The authored context requests each host view only when the author reads it.
use harness_strategy_sdk::{CDecimal, CExecutionSource, CMarketOrder, COrderSink, CStr, OrderSink};
use std::cell::Cell;
use std::ffi::c_void;

struct Host {
    reads: [Cell<usize>; 3],
}
unsafe extern "C" fn read(ctx: *const c_void, view: u32) -> CStr {
    let host = &*ctx.cast::<Host>();
    host.reads[view as usize].set(host.reads[view as usize].get() + 1);
    CStr::from_slice(if view == 0 {
        r#"{"timestamp_ns":17,"initial_cash":"100.00","cash":"100.00","fees":"0","equity":"100.00","realized_pnl":"0","unrealized_pnl":"0","gross_exposure":"0","positions":[]}"#
    } else {
        "[]"
    })
}
unsafe extern "C" fn submit(_: *mut c_void, _: *const CMarketOrder) -> i32 {
    unreachable!()
}
unsafe extern "C" fn next(_: *mut c_void) -> CStr {
    unreachable!()
}
unsafe extern "C" fn cancel(_: *mut c_void, _: CStr) -> i32 {
    unreachable!()
}
unsafe extern "C" fn target(_: *mut c_void, _: CStr, _: CDecimal) -> i32 {
    unreachable!()
}

#[test]
fn unused_account_and_histories_never_request_host_serialization() {
    let host = Host {
        reads: std::array::from_fn(|_| Cell::new(0)),
    };
    let sink = COrderSink {
        ctx: std::ptr::null_mut(),
        submit,
        next_order_id: next,
        cancel,
        target_position: target,
        execution: CExecutionSource {
            ctx: (&host as *const Host).cast(),
            read,
        },
    };
    let context = unsafe { OrderSink::from_sink(sink) };
    assert_eq!(host.reads.each_ref().map(Cell::get), [0, 0, 0]);
    assert_eq!(context.execution().timestamp_ns, 17);
    assert_eq!(context.execution().cash.to_string(), "100.00");
    assert_eq!(host.reads.each_ref().map(Cell::get), [1, 0, 0]);
    assert!(context.orders().is_empty());
    assert!(context.orders().is_empty());
    assert_eq!(host.reads.each_ref().map(Cell::get), [1, 1, 0]);
    assert!(context.closed_trades().is_empty());
    assert!(context.closed_trades().is_empty());
    assert_eq!(host.reads.each_ref().map(Cell::get), [1, 1, 1]);
}
