"""Ingest process command boundary."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ingest arguments without defining the later worker."""
    parser = argparse.ArgumentParser(
        prog="lake ingest",
        description="Run the market-lake ingest process.",
        epilog="The ingest worker is introduced by a later implementation task.",
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0
