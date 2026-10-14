"""Renewable ownership and ambient write fencing, independent of execution policy.

A lease is authority only while storage still recognizes its token. Local
threads and caches are never authority. Storage adapters commit their mutation
and the current fence together; external calls check authority before dispatch.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event, Thread
from typing import Protocol


class OwnershipLost(CancelledError):
    """This attempt must stop without publishing results or replaying effects."""


@dataclass(frozen=True, slots=True)
class Claim:
    workspace_id: str
    resource: str
    token: str
    fence: int


class OwnershipStore(Protocol):
    """Atomic lease operations; timestamps and monotonic fencing belong to storage."""

    def acquire(
        self, workspace_id: str, resource: str, seconds: float
    ) -> Claim | None: ...
    def renew(self, claim: Claim, seconds: float) -> None: ...
    def check(self, claim: Claim) -> None: ...
    def release(self, claim: Claim) -> None: ...


@dataclass(frozen=True, slots=True)
class HeldClaim:
    store: OwnershipStore
    claim: Claim
    lost: Event


_HELD: ContextVar[tuple[HeldClaim, ...]] = ContextVar("thesis_ownership", default=())


def held_claims() -> tuple[HeldClaim, ...]:
    """Return the fences every durable mutation of this attempt must honor."""
    return _HELD.get()


def assert_owned() -> None:
    """Fail closed before a model/tool dispatch when any enclosing owner is stale."""
    for held in _HELD.get():
        if held.lost.is_set():
            raise OwnershipLost("execution ownership was lost")
        held.store.check(held.claim)


@contextmanager
def own(store: OwnershipStore, claim: Claim, *, seconds: float = 60) -> Iterator[Claim]:
    """Keep an acquired claim alive and attach its fence to this execution context."""
    stop = Event()
    lost = Event()

    def heartbeat() -> None:
        while not stop.wait(seconds / 3):
            try:
                store.renew(claim, seconds)
            except Exception:
                lost.set()
                return

    thread = Thread(target=heartbeat, name="thesis-ownership", daemon=True)
    thread.start()
    token = _HELD.set((*_HELD.get(), HeldClaim(store, claim, lost)))
    try:
        assert_owned()
        yield claim
        assert_owned()
    finally:
        _HELD.reset(token)
        stop.set()
        thread.join(timeout=seconds / 3)
        if not lost.is_set():
            try:
                store.release(claim)
            except OwnershipLost:
                pass


@contextmanager
def mutation_guard(
    store: OwnershipStore, workspace_id: str, resource: str
) -> Iterator[None]:
    """Reentrant storage lock for bounded read/modify/write operations."""
    for held in _HELD.get():
        if held.store is store and (held.claim.workspace_id, held.claim.resource) == (
            workspace_id,
            resource,
        ):
            assert_owned()
            yield
            return
    # API handlers run in the thread pool; contention must not block the event loop.
    from time import monotonic, sleep

    deadline = monotonic() + 30
    while True:
        assert_owned()
        claim = store.acquire(workspace_id, resource, 60)
        if claim is not None:
            break
        if monotonic() >= deadline:
            raise TimeoutError(f"mutation ownership is busy: {resource}")
        sleep(0.025)
    with own(store, claim):
        yield
