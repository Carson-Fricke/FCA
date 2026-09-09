"""Quant-harness command-line facade."""

from harness.cli.application import app, main
from harness.mcp.__main__ import app as mcp_app

app.add_typer(mcp_app, name="mcp")

__all__ = ["app", "main"]
