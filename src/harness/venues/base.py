"""Execution venue boundary for the synchronous harness kernel."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.core.types.orders import OrderRequest


@runtime_checkable
class ExecutionVenue(Protocol):
    """Fire-and-forget execution commands whose outcomes arrive as events.

    Implementations publish execution reports, fills, and position snapshots
    through the engine's event bus. Command return values never carry venue
    outcomes, which keeps simulated and live adapters on the same boundary.
    """

    def submit(self, order: OrderRequest) -> None:
        """Submit an order and return before any outcome is delivered."""
        ...

    def cancel(self, order_id: str) -> None:
        """Request cancellation; confirmation or rejection arrives as an event."""
        ...

    def replace(self, order_id: str, replacement: OrderRequest) -> None:
        """Request atomic replacement of an existing order."""
        ...

    def request_position_snapshot(self, account_id: str) -> None:
        """Request current positions for an account through the event bus."""
        ...


__all__ = ["ExecutionVenue"]
