"""Published Uvicorn application target."""

from lake.hosted.runtime import hosted_overrides
from lake.http.application import create_app

app = create_app(**hosted_overrides())

__all__ = ["app", "create_app"]
