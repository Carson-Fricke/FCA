"""Durable runnable references and leased execution; no model or workflow policy.

API admission commits messages and runnable references together. Workers can run on any host.
An attempt which loses ownership is interrupted, never automatically replayed;
new input is independent work and observes the retained transcript and jobs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Protocol

from thesis_runtime.storage.ownership import OwnershipLost, OwnershipStore, own


@dataclass(frozen=True, slots=True)
class Work:
    workspace_id: str
    key: str
    resource: str
    kind: str
    payload: Mapping[str, object]
    state: str = "pending"
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class WorkReference:
    """Compact notification; hydrate executable payload only after claiming it."""

    workspace_id: str
    key: str
    resource: str
    created_at: str


class WorkStore(Protocol):
    """Create-only work identities and fenced attempt state."""

    def enqueue(self, work: Work) -> None: ...
    def ready(self, limit: int | None = 64) -> tuple[Work | WorkReference, ...]: ...
    def start(self) -> tuple[WorkReference, ...]: ...
    def wait(self, timeout: float) -> tuple[WorkReference, ...]:
        """Return bounded durable notifications, redelivering after restart."""
        ...

    def active(self, workspace_id: str, resource: str) -> bool: ...
    def get(self, workspace_id: str, key: str) -> Work: ...
    def transition(self, work: Work, state: str) -> None: ...


class DurableWorker:
    """Execute one resource at a time across all replicas, with renewable fencing."""

    def __init__(
        self,
        store: WorkStore,
        ownership: OwnershipStore,
        handlers: Mapping[str, Callable[[Work], None]],
        interrupted: Callable[[Work], None],
        repair: Callable[[], bool | None] | None = None,
    ) -> None:
        self.store = store
        self.ownership = ownership
        self.handlers = dict(handlers)
        self.interrupted = interrupted
        self.stopped = Event()
        self.repair = repair

    def tick(self, candidates: tuple[Work | WorkReference, ...] | None = None) -> int:
        """Bounded worker pass; live owners are untouched by another process."""
        completed = 0
        for candidate in self.store.ready() if candidates is None else candidates:
            if self.stopped.is_set():
                break
            claim = self.ownership.acquire(
                candidate.workspace_id, candidate.resource, 60
            )
            if claim is None:
                continue
            try:
                with own(self.ownership, claim):
                    work = self.store.get(candidate.workspace_id, candidate.key)
                    if work.state == "running":
                        # The old attempt's token expired before it could finish.
                        # Fencing is acquired before publishing interruption evidence.
                        self.interrupted(work)
                        self.store.transition(work, "interrupted")
                    elif work.state == "pending":
                        self.store.transition(work, "running")
                        try:
                            self.handlers[work.kind](work)
                        except OwnershipLost:
                            raise
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "durable work interrupted: %s", work.key
                            )
                            self.interrupted(work)
                            self.store.transition(work, "interrupted")
                        else:
                            self.store.transition(work, "completed")
                    completed += 1
            except OwnershipLost:
                # A new worker will record the unknown attempt; the stale one
                # has no authority to publish failure, success, or a retry.
                continue
        return completed

    def run(self) -> None:
        """Dispatch notifications; reconcile missed work and expired owners slowly.

        Notifications may repeat and replicas may see the same reference. Keep
        busy/unclaimed references until they run: advancing a feed cursor must
        never discard work because the pool or another resource owner was busy.
        """
        from time import monotonic

        active: dict[tuple[str, str], tuple[Future[int], Work | WorkReference]] = {}
        pending: dict[tuple[str, str], Work | WorkReference] = {}
        retry_at: dict[tuple[str, str], float] = {}
        initialized = False
        next_reconcile = 0.0
        next_repair = 0.0
        next_discovery = 0.0
        logger = logging.getLogger(__name__)
        discovery: Future[tuple[WorkReference, ...]] | None = None
        reconciliation: Future[tuple[Work | WorkReference, ...]] | None = None
        repair: Future[bool | None] | None = None
        with (
            ThreadPoolExecutor(max_workers=8, thread_name_prefix="thesis-work") as pool,
            ThreadPoolExecutor(
                max_workers=3, thread_name_prefix="thesis-recovery"
            ) as background,
        ):
            while not self.stopped.is_set():
                now = monotonic()
                if discovery is not None and discovery.done():
                    try:
                        pending.update(
                            ((work.workspace_id, work.key), work)
                            for work in discovery.result()
                        )
                        if not initialized:
                            # The feed cursor must precede a retained snapshot.
                            # Repeat an early scan which raced feed initialization.
                            next_reconcile = 0.0
                            initialized = True
                    except Exception:
                        logger.exception(
                            "durable notification read failed; cursor remains recoverable"
                        )
                        next_discovery = now + 1
                    discovery = None
                if reconciliation is not None and reconciliation.done():
                    try:
                        batch = reconciliation.result()
                        pending.update(
                            ((work.workspace_id, work.key), work) for work in batch
                        )
                        next_reconcile = now + (1 if len(batch) == 64 else 60)
                    except Exception:
                        logger.exception(
                            "durable work reconciliation failed; queued work remains retained"
                        )
                        next_reconcile = now + 60
                    reconciliation = None
                if repair is not None and repair.done():
                    try:
                        more = repair.result()
                    except Exception:
                        more = False
                        logger.exception(
                            "durable recovery pass failed; queued work remains retained"
                        )
                    repair = None
                    # Continue a bounded sweep promptly, without overlapping it.
                    next_repair = now + (1 if more else 60)
                if discovery is None and now >= next_discovery and len(pending) < 256:
                    discovery = (
                        background.submit(self.store.wait, 0.25)
                        if initialized
                        else background.submit(self.store.start)
                    )
                if (
                    reconciliation is None
                    and now >= next_reconcile
                    and len(pending) < 256
                ):
                    reconciliation = background.submit(self.store.ready, 64)
                if self.repair is not None and repair is None and now >= next_repair:
                    repair = background.submit(self.repair)
                for resource, (future, work) in tuple(active.items()):
                    if not future.done():
                        continue
                    del active[resource]
                    key = (work.workspace_id, work.key)
                    try:
                        finished = future.result()
                    except Exception:
                        logger.exception(
                            "durable attempt failed; retained work will be reconciled"
                        )
                        finished = 0
                    if not finished:
                        pending[key] = work
                        retry_at[key] = monotonic() + 5
                for key, work in sorted(
                    pending.items(), key=lambda item: (item[1].created_at, item[1].key)
                ):
                    resource = (work.workspace_id, work.resource)
                    if resource in active or monotonic() < retry_at.get(key, 0):
                        continue
                    if len(active) >= 8:
                        break
                    del pending[key]
                    retry_at.pop(key, None)
                    active[resource] = (pool.submit(self.tick, (work,)), work)
                self.stopped.wait(0.05)

    def stop(self) -> None:
        self.stopped.set()
