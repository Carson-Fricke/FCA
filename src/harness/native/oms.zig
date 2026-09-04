//! Execution-report-driven order management, ported from
//! ``src/harness/oms/state.py`` and ``src/harness/oms/manager.py``.
//!
//! This is a stable facade: the implementation lives in two focused modules —
//! ``oms_state.zig`` (the state machine and its transition/ledger tables) and
//! ``oms_manager.zig`` (the ``SingleLegOMS`` lifecycle registry) — with the
//! shared fixed-point kernel in ``decimal.zig``. Every public name keeps its
//! historical location on this namespace, so ``books.zig`` and external
//! consumers continue to use ``oms.dec``, ``oms.Side``, and ``oms.SingleLegOMS``
//! unchanged.

pub const decimal = @import("decimal.zig");
pub const state = @import("oms_state.zig");
pub const manager = @import("oms_manager.zig");

pub const dec = decimal.dec;

pub const Error = state.Error;
pub const Side = state.Side;
pub const OrderType = state.OrderType;
pub const TimeInForce = state.TimeInForce;
pub const OrderStatus = state.OrderStatus;
pub const ExecutionType = state.ExecutionType;
pub const SingleLegOrder = state.SingleLegOrder;
pub const ExecutionReport = state.ExecutionReport;
pub const OrderSnapshot = state.OrderSnapshot;
pub const isTerminalStatus = state.isTerminalStatus;
pub const executionStatuses = state.executionStatuses;
pub const containsStatus = state.containsStatus;
pub const OrderStateMachine = state.OrderStateMachine;

pub const ReplaceStatus = manager.ReplaceStatus;
pub const SessionAction = manager.SessionAction;
pub const ReplaceRecord = manager.ReplaceRecord;
pub const SessionDirective = manager.SessionDirective;
pub const SingleLegOMS = manager.SingleLegOMS;

comptime {
    _ = decimal;
    _ = state;
    _ = manager;
}
