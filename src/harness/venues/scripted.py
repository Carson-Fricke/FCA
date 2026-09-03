"""Deterministic execution venue driven by an explicit FIFO script."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from harness.core.events import EventBus
from harness.core.types.common import text
from harness.core.types.events import Event
from harness.core.types.orders import MultiLegOrder, OrderRequest, SingleLegOrder


class VenueOperation(StrEnum):
    """Commands understood by an execution venue."""

    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"
    POSITION_SNAPSHOT = "position_snapshot"


@dataclass(frozen=True, slots=True)
class VenueCall:
    """Normalized identity of one command received by a scripted venue."""

    operation: VenueOperation
    order_id: str | None = None
    replacement_order_id: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", VenueOperation(self.operation))
        for field_name in ("order_id", "replacement_order_id", "account_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, text(value, field_name))
        expected_fields = {
            VenueOperation.SUBMIT: (True, False, False),
            VenueOperation.CANCEL: (True, False, False),
            VenueOperation.REPLACE: (True, True, False),
            VenueOperation.POSITION_SNAPSHOT: (False, False, True),
        }
        populated_fields = (
            self.order_id is not None,
            self.replacement_order_id is not None,
            self.account_id is not None,
        )
        if populated_fields != expected_fields[self.operation]:
            raise ValueError(
                f"{self.operation.value} call has invalid order/account identifiers"
            )


@dataclass(frozen=True, slots=True)
class ScriptedStep:
    """An expected venue call and the event outcomes it schedules."""

    call: VenueCall
    outcomes: tuple[Event, ...] | Sequence[Event] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.call, VenueCall):
            raise TypeError("call must be a VenueCall")
        outcomes = tuple(self.outcomes)
        if not all(isinstance(outcome, Event) for outcome in outcomes):
            raise TypeError("outcomes must contain Event values")
        object.__setattr__(self, "outcomes", outcomes)


class UnexpectedVenueCall(AssertionError):
    """Raised when a call does not match the next scripted step."""


class ScriptedVenue:
    """Schedule prebuilt outcomes for an exact sequence of venue commands.

    The script owns no clock, random source, or network resource. Event
    timestamps and sequences are supplied by the scenario, and the existing
    :class:`EventBus` determines callback order.
    """

    __slots__ = ("_bus", "_calls", "_steps")

    def __init__(self, bus: EventBus, steps: Iterable[ScriptedStep] = ()) -> None:
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        normalized_steps = tuple(steps)
        if not all(isinstance(step, ScriptedStep) for step in normalized_steps):
            raise TypeError("steps must contain ScriptedStep values")
        self._bus = bus
        self._steps = deque(normalized_steps)
        self._calls: list[VenueCall] = []

    @property
    def calls(self) -> tuple[VenueCall, ...]:
        """Return successfully matched calls in receipt order."""
        return tuple(self._calls)

    @property
    def remaining_steps(self) -> int:
        """Return the number of expected calls not yet received."""
        return len(self._steps)

    def submit(self, order: OrderRequest) -> None:
        """Match a submission and schedule its scripted outcomes."""
        self._validate_order(order, "order")
        self._execute(VenueCall(VenueOperation.SUBMIT, order_id=order.order_id))

    def cancel(self, order_id: str) -> None:
        """Match a cancellation and schedule its scripted outcomes."""
        self._execute(VenueCall(VenueOperation.CANCEL, order_id=order_id))

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        """Match a replacement and schedule its scripted outcomes."""
        self._validate_order(replacement, "replacement")
        self._execute(
            VenueCall(
                VenueOperation.REPLACE,
                order_id=order_id,
                replacement_order_id=replacement.order_id,
            )
        )

    def request_position_snapshot(self, account_id: str) -> None:
        """Match a snapshot request and schedule its position events."""
        self._execute(
            VenueCall(VenueOperation.POSITION_SNAPSHOT, account_id=account_id)
        )

    def assert_exhausted(self) -> None:
        """Assert that every scripted call has been consumed."""
        if self._steps:
            raise AssertionError(
                f"{len(self._steps)} scripted venue call(s) remain; "
                f"next expected call is {self._steps[0].call!r}"
            )

    def _execute(self, call: VenueCall) -> None:
        if not self._steps:
            raise UnexpectedVenueCall(
                f"unexpected venue call {call!r}; no scripted calls remain"
            )
        step = self._steps[0]
        if call != step.call:
            raise UnexpectedVenueCall(
                f"unexpected venue call {call!r}; expected {step.call!r}"
            )
        self._steps.popleft()
        self._calls.append(call)
        for outcome in step.outcomes:
            self._bus.schedule(outcome)

    @staticmethod
    def _validate_order(order: OrderRequest, field_name: str) -> None:
        if not isinstance(order, (SingleLegOrder, MultiLegOrder)):
            raise TypeError(f"{field_name} must be an OrderRequest")


__all__ = [
    "ScriptedStep",
    "ScriptedVenue",
    "UnexpectedVenueCall",
    "VenueCall",
    "VenueOperation",
]
