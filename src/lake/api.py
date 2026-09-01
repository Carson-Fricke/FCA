"""API process command boundary."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse API process arguments without defining the later API service."""
    parser = argparse.ArgumentParser(
        prog="lake api",
        description="Run the market-lake API process.",
        epilog="The API service is introduced by a later implementation task.",
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0
