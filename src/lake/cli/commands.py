"""Top-level market-lake command dispatch."""

import argparse
from collections.abc import Callable, Sequence

from lake.cli import api, ingest, local

Command = Callable[[Sequence[str] | None], int]

_COMMANDS: dict[str, Command] = {
    "api": api.main,
    "ingest": ingest.main,
    "local": local.main,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lake",
        description="Run a market-lake process from the shared runtime image.",
    )
    commands = parser.add_subparsers(dest="command", metavar="{api,ingest,local}")
    commands.add_parser("api", add_help=False, help="run the API process")
    commands.add_parser("ingest", add_help=False, help="run the ingest process")
    commands.add_parser("local", add_help=False, help="run local API, ingest, or reset")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Select a process command and pass through its remaining arguments."""
    parser = _parser()
    arguments, remaining = parser.parse_known_args(argv)
    command = arguments.command
    if command is None:
        if remaining:
            parser.error(f"unrecognized arguments: {' '.join(remaining)}")
        parser.print_help()
        return 0
    if not isinstance(command, str):
        parser.error("invalid command")
    return _COMMANDS[command](remaining)
