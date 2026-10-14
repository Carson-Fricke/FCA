"""Shared process-local serialization for storage adapters; durable leases own authority."""

from threading import Lock, RLock

_CONVERSATION_LOCKS: dict[tuple[str, str, str], RLock] = {}
_LOCK_REGISTRY_GUARD = Lock()


def conversation_lock(store: str, workspace_id: str, root: str) -> RLock:
    """Share a single-process write/delete guard across adapter instances."""
    with _LOCK_REGISTRY_GUARD:
        return _CONVERSATION_LOCKS.setdefault((store, workspace_id, root), RLock())
