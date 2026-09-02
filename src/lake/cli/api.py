"""API process command boundary."""

import argparse
from collections.abc import Sequence

import uvicorn


def main(argv: Sequence[str] | None = None) -> int:
    """Run the market-lake FastAPI application with Uvicorn."""
    parser = argparse.ArgumentParser(
        prog="lake api",
        description="Run the market-lake API process.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run("lake.app:app", host=arguments.host, port=arguments.port)
    return 0
