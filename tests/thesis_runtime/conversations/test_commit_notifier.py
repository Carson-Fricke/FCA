"""Commit-notification wake semantics for the timeline stream."""

from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from thesis_runtime.conversations.streaming import CommitNotifier


def test_notify_before_wait_is_not_lost() -> None:
    notifier = CommitNotifier()
    version = notifier.version("workspace", "chat")
    notifier.notify("workspace", "chat")
    assert notifier.wait("workspace", "chat", version, 1.0) != version


def test_wait_wakes_on_notification_without_timeout() -> None:
    notifier = CommitNotifier()
    version = notifier.version("workspace", "chat")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(notifier.wait, "workspace", "chat", version, 5.0)
        notifier.notify("workspace", "chat")
        started = monotonic()
        assert pending.result(timeout=2) != version
        assert monotonic() - started < 2.0


def test_unrelated_chat_notification_does_not_wake_reader() -> None:
    notifier = CommitNotifier()
    version = notifier.version("workspace", "chat")
    notifier.notify("workspace", "other")
    assert notifier.wait("workspace", "chat", version, 0.02) == version
