"""Stable local market-lake process and ingest commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date

import uvicorn

from lake.local import (
    LocalIngestError,
    LocalLayout,
    LocalStateError,
    create_local_app,
    run_local_ingest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lake local",
        description="Run the market lake with local files and no Azure resources.",
    )
    commands = parser.add_subparsers(
        dest="local_command",
        required=True,
        metavar="{api,ingest,reset}",
    )

    api = commands.add_parser("api", help="serve the API over local state")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    _add_data_dir(api)

    ingest = commands.add_parser(
        "ingest", help="fetch one closed Alpaca equity session"
    )
    ingest.add_argument(
        "--symbol", help="configured equity symbol; defaults to the first soak symbol"
    )
    ingest.add_argument(
        "--session-date",
        type=date.fromisoformat,
        help="closed XNYS session date; defaults to the latest closed session",
    )
    _add_data_dir(ingest)

    reset = commands.add_parser("reset", help="delete generated local state")
    reset.add_argument(
        "--yes", action="store_true", help="confirm deletion without prompting"
    )
    _add_data_dir(reset)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Dispatch a local command without constructing any Azure client."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        layout = LocalLayout.from_environment(arguments.data_dir, environ=environment)
        if arguments.local_command == "api":
            if not 1 <= arguments.port <= 65_535:
                return _fail("--port must be between 1 and 65535")
            token = _required(environment, "LAKE_API__BEARER_TOKEN")
            application = create_local_app(layout=layout, bearer_token=token)
            uvicorn.run(application, host=arguments.host, port=arguments.port)
            return 0
        if arguments.local_command == "ingest":
            result = asyncio.run(
                run_local_ingest(
                    layout=layout,
                    symbol=arguments.symbol,
                    session_date=arguments.session_date,
                    environ=environment,
                )
            )
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if not arguments.yes:
            return _fail("reset requires --yes")
        removed = layout.reset()
        print(
            json.dumps(
                {"data_root": str(layout.root), "removed": removed},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    except (LocalIngestError, LocalStateError, OSError, ValueError) as error:
        return _fail(str(error))


def _add_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        help="local state root; defaults to LAKE_LOCAL_DATA_DIR or .local/market-lake",
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise LocalStateError(f"{name} is required for the local API")
    return value


def _fail(message: str) -> int:
    print(f"lake local: error: {message}", file=sys.stderr)
    return 2


__all__ = ["main"]
