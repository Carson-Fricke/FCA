"""Shared fakes for the thin screener client tests.

A :class:`FakeToolProvider` scripts the five screening MCP tools without any
transport, and the fixtures build canonical definitions and frozen
``fca-harness-screen-v1`` documents so every test exercises the real importer
and service code paths.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

import pytest

from thesis.integrations.mcp import McpToolResult
from thesis.screening import ScreenerDefinition

WORKSPACE = "alpha-lab"
RUN_ID = "scr-run-001"
AS_OF = "2026-09-28"


def make_definition(**overrides: object) -> ScreenerDefinition:
    values: dict[str, object] = {
        "name": "Momentum leaders",
        "lookback_days": 252,
        "bar_interval": "1d",
        "rank_metric": "momentum_12_1",
        "universe_revision": "sp500-2026-09",
        "source": "fn screen(ctx) { ctx.rows() }",
        "parameters": {"threshold": 0.05, "sectors": ["tech", "energy"]},
    }
    values.update(overrides)
    return ScreenerDefinition(**values)  # type: ignore[arg-type]


def make_screen_document(**overrides: object) -> dict[str, object]:
    definition = make_definition()
    document: dict[str, object] = {
        "schema": "fca-harness-screen-v1",
        "status": "complete",
        "run_id": RUN_ID,
        "as_of": AS_OF,
        "definition_id": definition.definition_id,
        "definition_hash": definition.definition_sha256,
        "bundle_hash": definition.source_sha256,
        "rank_metric": definition.rank_metric,
        "bar_interval": definition.bar_interval,
        "data_window": {"start": "2025-09-28", "end": "2026-09-28"},
        "universe_revision": definition.universe_revision,
        "rows": [
            {"symbol": "AAPL", "metrics": {"momentum_12_1": "0.44"}},
            {"symbol": "MSFT", "metrics": {"momentum_12_1": "0.31"}},
        ],
        "provenance": {
            "source_digest": definition.source_sha256,
            "toolchain_digest": "c" * 64,
            "run_digest": "d" * 64,
        },
    }
    document.update(overrides)
    return document


class FakeToolProvider:
    """Scripted screening MCP tool provider with no transport."""

    def __init__(
        self,
        *,
        run_id: str = RUN_ID,
        states: Sequence[str] = ("running", "complete"),
        screen_document: Mapping[str, object] | None = None,
        catalog: Mapping[str, object] | None = None,
        artifact_present: bool = True,
        artifact_readable: bool = True,
        artifact_encoding: str = "utf-8",
        artifact_content: str | None = None,
        launch_error: bool = False,
        status_error: bool = False,
        status_alias: bool = False,
    ) -> None:
        self.run_id = run_id
        self.states = list(states)
        self.screen_document = (
            make_screen_document() if screen_document is None else dict(screen_document)
        )
        self.catalog = (
            {"run_id": run_id, "status": "complete"}
            if catalog is None
            else dict(catalog)
        )
        self.artifact_present = artifact_present
        self.artifact_readable = artifact_readable
        self.artifact_encoding = artifact_encoding
        self.artifact_content = artifact_content
        self.launch_error = launch_error
        self.status_error = status_error
        self.status_alias = status_alias
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.configs: list[dict[str, object]] = []

    def run_screener(
        self, *, config: Mapping[str, object], workspace: str | None = None
    ) -> McpToolResult:
        self.configs.append(dict(config))
        self.calls.append(
            ("run_screener", {"config": dict(config), "workspace": workspace})
        )
        if self.launch_error:
            return McpToolResult(
                tool="run_screener",
                is_error=True,
                structured={},
                text="harness offline",
            )
        state = self.states[0] if self.states else "complete"
        return McpToolResult(
            tool="run_screener",
            is_error=False,
            structured={"run_id": self.run_id, "state": state},
        )

    def get_screener_status(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("get_screener_status", {"job_id": job_id}))
        if self.status_error:
            return McpToolResult(
                tool="get_screener_status",
                is_error=True,
                structured={},
                text="status unavailable",
            )
        state = self.states.pop(0) if self.states else "complete"
        key = "status" if self.status_alias else "state"
        return McpToolResult(
            tool="get_screener_status",
            is_error=False,
            structured={"job_id": job_id, key: state},
        )

    def get_run(self, *, run_id: str) -> McpToolResult:
        self.calls.append(("get_run", {"run_id": run_id}))
        return McpToolResult(
            tool="get_run", is_error=False, structured=dict(self.catalog)
        )

    def list_artifacts(self, *, run_id: str) -> McpToolResult:
        self.calls.append(("list_artifacts", {"run_id": run_id}))
        entries: list[dict[str, object]] = [
            {"name": "config.yaml", "sha256": "b" * 64, "size_bytes": 16}
        ]
        if self.artifact_present:
            entries.append(
                {"name": "screen.json", "sha256": "a" * 64, "size_bytes": 128}
            )
        return McpToolResult(
            tool="list_artifacts",
            is_error=False,
            structured={"run_id": run_id, "artifacts": entries},
        )

    def read_artifact(self, *, run_id: str, name: str) -> McpToolResult:
        self.calls.append(("read_artifact", {"run_id": run_id, "name": name}))
        if not self.artifact_readable:
            return McpToolResult(
                tool="read_artifact",
                is_error=True,
                structured={},
                text="artifact unreadable",
            )
        content = json.dumps(self.screen_document)
        if self.artifact_content is not None:
            content = self.artifact_content
        if self.artifact_encoding == "hex":
            content = content.encode("utf-8").hex()
        return McpToolResult(
            tool="read_artifact",
            is_error=False,
            structured={"encoding": self.artifact_encoding, "content": content},
        )

    def tool_names(self) -> tuple[str, ...]:
        """Return the tools called so far, in order."""
        return tuple(name for name, _ in self.calls)


def collect_sleeps() -> tuple[list[float], Callable[[float], None]]:
    """Return a sleep recorder and the matching injectable sleep callable."""
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)

    return slept, sleep


@pytest.fixture()
def fake_provider() -> type[FakeToolProvider]:
    return FakeToolProvider


@pytest.fixture()
def definition_factory() -> Callable[..., ScreenerDefinition]:
    return make_definition


@pytest.fixture()
def screen_document_factory() -> Callable[..., dict[str, object]]:
    return make_screen_document


@pytest.fixture()
def sleep_recorder() -> Callable[[], tuple[list[float], Callable[[float], None]]]:
    return collect_sleeps
