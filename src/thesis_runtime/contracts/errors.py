"""Transport-neutral delivery, provider and extension catalog failures."""

from collections.abc import Mapping


class DeliveryError(ValueError):
    """An incoming delivery has invalid content, scope or identity."""


class ToolCatalogError(RuntimeError):
    def __init__(self, outcome: Mapping[str, object]) -> None:
        self.outcome = dict(outcome)
        super().__init__(str(outcome.get("message", "tool catalog unavailable")))


class ProviderUnreachableError(RuntimeError):
    """Network-level failure while contacting a model provider."""

    recoverable = True


class ProviderRejectedError(RuntimeError):
    """The provider refused a request or returned an unusable response."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        transient: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.recoverable = (
            status in (408, 429) or 500 <= status <= 599
            if status is not None
            else transient
        )
