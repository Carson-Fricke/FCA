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
    def ready(self, limit: int | None = 64) -> tuple[Work, ...]: ...
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
        repair: Callable[[], None] | None = None,
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
        next_reconcile = monotonic() + 60
        next_repair = 0.0
        logger = logging.getLogger(__name__)
        with ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="thesis-work"
        ) as pool:
            while not self.stopped.is_set():
                now = monotonic()
                if not initialized:
                    try:
                        # Capture server feed progress before the retained-work
                        # snapshot. Their overlap prevents an admission gap.
                        pending.update(
                            ((work.workspace_id, work.key), work)
                            for work in self.store.start()
                        )
                        initialized = True
                    except Exception:
                        logger.exception("durable startup reconciliation failed")
                        self.stopped.wait(1)
                        continue
                if self.repair is not None and now >= next_repair:
                    try:
                        self.repair()
                    except Exception:
                        logger.exception(
                            "durable recovery pass failed; queued work remains retained"
                        )
                    next_repair = monotonic() + 60
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
                try:
                    candidates: tuple[Work | WorkReference, ...] = self.store.wait(
                        0.1 if active else 0.25
                    )
                    if monotonic() >= next_reconcile:
                        candidates += self.store.ready()
                        next_reconcile = monotonic() + 60
                    for work in candidates:
                        pending[(work.workspace_id, work.key)] = work
                except Exception:
                    logger.exception(
                        "durable notification read failed; cursor remains recoverable"
                    )
                    self.stopped.wait(1)
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

    def stop(self) -> None:
        self.stopped.set()
