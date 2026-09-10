"""The ``harness mcp`` command: serve the MCP server over stdio.

Composition follows the process-boundary conventions of the other harness
commands: the local process runner spawns ``harness backtest-job`` /
``harness trader`` through :mod:`subprocess`, the remote boundary speaks
HTTP through ``httpx``, and sweeps run through the programmatic
:class:`harness.sweeps.runner.SweepRunner` with an in-process launcher over
an in-memory scratch store. The remote harness API base URL comes from
``--api-base-url`` or the ``HARNESS_API_BASE_URL`` environment variable;
query tools (job status, runs, artifacts) use it whenever it is configured,
while ``--mode remote`` additionally routes ``run_backtest`` launches
through it. Heavy composition is imported lazily so ``harness --help``
stays cheap.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from harness.mcp.auth import EnvironmentBearerTokenSource
from harness.mcp.server import McpServer, create_server
from harness.mcp.tools import (
    HarnessToolsContext,
    ProcessOutcome,
    RemoteResponse,
    SweepSubmitter,
    ToolCapabilities,
)
from harness.mcp.transport import serve_stdio

app = typer.Typer(
    add_completion=False,
    help="Serve the harness MCP server over stdio.",
    invoke_without_command=True,
    no_args_is_help=False,
)


class HttpxRemoteTransport:
    """Remote harness API boundary over HTTP with decoded JSON bodies."""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        import httpx

        self._client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def get(self, url: str, headers: Mapping[str, str]) -> RemoteResponse:
        """Perform one authenticated GET and decode its JSON body."""
        response = self._client.get(url, headers=dict(headers))
        return RemoteResponse(
            response.status_code, dict(response.headers), _json_body(response)
        )

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> RemoteResponse:
        """Perform one authenticated POST with a JSON body."""
        response = self._client.post(url, headers=dict(headers), json=payload)
        return RemoteResponse(
            response.status_code, dict(response.headers), _json_body(response)
        )


def _json_body(response: object) -> Mapping[str, object]:
    body = getattr(response, "json", None)
    if not callable(body):
        return {}
    try:
        decoded = body()
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def subprocess_runner(argv: Sequence[str]) -> ProcessOutcome:
    """Run one local harness process and capture its observed outcome."""
    completed = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return ProcessOutcome(completed.returncode, completed.stdout, completed.stderr)


def sweep_submitter() -> SweepSubmitter:
    """Compose the local sweeps surface with an in-process launcher."""

    def submit(spec_path: str) -> Mapping[str, object]:
        from harness.http.launcher import (
            InProcessBacktestLauncher,
            parse_backtest_configuration,
        )
        from harness.results.store import (
            BacktestResultsStore,
            InMemoryRunBlobStore,
            InMemoryRunCatalogRepository,
        )
        from harness.sweeps.runner import SweepRunner
        from harness.sweeps.spec import SweepSpec
        from lake.catalog.memory import InMemoryJobRepository

        document = json.loads(
            Path(spec_path).read_text(encoding="utf-8"), parse_float=Decimal
        )
        if not isinstance(document, dict):
            raise ValueError("sweep spec document must be a JSON object")
        base = parse_backtest_configuration(document.get("base") or {})
        instance_id = document.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("sweep spec requires an instance_id string")
        space = document.get("space")
        if not isinstance(space, dict):
            raise ValueError("sweep spec requires a space mapping")
        space_levels = {
            str(name): tuple(values)
            for name, values in space.items()
            if isinstance(values, (list, tuple))
        }
        mode = document.get("mode", "grid")
        if mode == "grid":
            spec = SweepSpec.grid(base, instance_id, space_levels)
        elif mode == "random":
            spec = SweepSpec.random(
                base,
                instance_id,
                space_levels,
                count=int(document["count"]),
                seed=int(document["seed"]),
            )
        else:
            raise ValueError(f"sweep spec mode must be 'grid' or 'random': {mode!r}")
        blobs = InMemoryRunBlobStore()
        store = BacktestResultsStore(blobs, InMemoryRunCatalogRepository())
        launcher = InProcessBacktestLauncher(store, InMemoryJobRepository())
        submission = SweepRunner(launcher, store, blobs).submit(spec)
        return {
            "sweep_id": submission.sweep_id,
            "requested": submission.requested,
            "expanded": submission.expanded,
            "duplicates_skipped": submission.duplicates_skipped,
            "run_ids": list(submission.run_ids),
        }

    return submit


def build_server(
    *,
    mode: str,
    base_url: str | None,
    enable_trader: bool,
) -> McpServer:
    """Compose the MCP server from the documented deployment settings."""
    resolved_base = (
        base_url
        if base_url is not None and base_url.strip()
        else os.environ.get("HARNESS_API_BASE_URL")
    )
    context = HarnessToolsContext(
        mode=mode,
        credentials=EnvironmentBearerTokenSource(),
        capabilities=ToolCapabilities(trader=bool(enable_trader)),
        api_base_url=resolved_base,
        remote_transport=HttpxRemoteTransport() if resolved_base else None,
        runner=subprocess_runner,
        sweep_submitter=sweep_submitter(),
    )
    return create_server(context)


@app.callback(invoke_without_command=True)
def serve(
    context: typer.Context,
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="run_backtest boundary: 'local' process or 'remote' API launch.",
        ),
    ] = "local",
    api_base_url: Annotated[
        str | None,
        typer.Option(
            "--api-base-url",
            help="Remote harness API base URL (defaults to HARNESS_API_BASE_URL).",
        ),
    ] = None,
    enable_trader: Annotated[
        bool,
        typer.Option(
            "--enable-trader/--no-enable-trader",
            help="Expose the capability-gated launch_trader tool.",
        ),
    ] = False,
) -> None:
    """Serve the harness MCP server over stdio until stdin closes."""
    if context.invoked_subcommand is not None:
        return
    server = build_server(
        mode=mode,
        base_url=api_base_url,
        enable_trader=enable_trader,
    )
    serve_stdio(server)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone harness MCP command.

    When the Rust entrypoint flip is enabled and a Rust ``mcp`` binary is
    available, this dispatches to it; otherwise it runs the Python command.
    """
    from harness.cli.rust_entrypoints import exec_rust_entrypoint

    rust_result = exec_rust_entrypoint("mcp", [] if argv is None else list(argv))
    if rust_result is not None:
        return rust_result
    try:
        app(args=None if argv is None else list(argv), prog_name="harness-mcp")
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
