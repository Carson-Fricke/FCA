"""Bounded, cooperative backoff for a fresh model request, never tool replay."""

from collections.abc import Callable
from time import sleep

from thesis_runtime.storage.ownership import assert_owned

RECOVERY_DELAYS = (1.0, 2.0, 4.0)
MAX_RECOVERY_DELAY = 60.0


def wait_for_recovery(
    delay: float,
    *,
    cancelled: Callable[[], bool] | None,
    steered: Callable[[], bool] | None,
) -> None:
    """Yield to cancellation/steering; ownership loss always escapes recovery."""
    remaining = delay
    while remaining > 0:
        assert_owned()
        if (cancelled is not None and cancelled()) or (
            steered is not None and steered()
        ):
            return
        interval = min(0.25, remaining)
        sleep(interval)
        remaining -= interval
    assert_owned()
