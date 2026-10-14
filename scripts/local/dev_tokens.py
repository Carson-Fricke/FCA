"""Ensure the local stack's bearer tokens exist in the ignored ``.env`` file.

The loopback stack authenticates three process boundaries with static bearers:
market-lake (``LAKE_API__BEARER_TOKEN``), quant-harness
(``HARNESS_API_BEARER_TOKEN``), and Thesis (``THESIS_API__BEARER_TOKEN``).  The
Svelte UI presents the Thesis token as ``PUBLIC_THESIS_API_BEARER_TOKEN``.
Generating a fresh value per process start invalidates every peer's credential
on restart, so this helper mints each token once, stores it in ``.env``, and
reuses it thereafter.  ``scripts/local/start-stack.ps1`` runs it before every
launch, which keeps a restart token-stable.

Dispositions per variable are reported as ``generated`` (new value minted),
``adopted`` (recovered from the process environment), ``reused`` (kept from
``.env``), or ``derived`` (mirrored from another variable).  Values are never
printed.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

MANAGED_TOKENS: tuple[str, ...] = (
    "LAKE_API__BEARER_TOKEN",
    "HARNESS_API_BEARER_TOKEN",
    "THESIS_API__BEARER_TOKEN",
)

DERIVED_TOKENS: tuple[tuple[str, str], ...] = (
    ("PUBLIC_THESIS_API_BEARER_TOKEN", "THESIS_API__BEARER_TOKEN"),
)

_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<rest>.*)$"
)


@dataclass(frozen=True, slots=True)
class EnsureResult:
    """The resolved token values and how each was obtained."""

    values: Mapping[str, str]
    dispositions: Mapping[str, str]


def _default_token() -> str:
    return secrets.token_urlsafe(32)


def _read_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match is not None:
            values[match.group("key")] = match.group("rest").strip()
    return values


def _render(text: str, updates: Mapping[str, str]) -> str:
    lines = text.splitlines()
    applied: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match is not None and match.group("key") in updates:
            key = match.group("key")
            rendered.append(f"{key}={updates[key]}")
            applied.add(key)
        else:
            rendered.append(line)
    missing = [key for key in updates if key not in applied]
    if missing:
        rendered.append("# Local development bearer tokens.")
        rendered.append("# Managed by scripts/local/dev_tokens.py; do not commit.")
        for key in missing:
            rendered.append(f"{key}={updates[key]}")
    return "\n".join(rendered) + "\n"


def ensure_tokens(
    env_file: Path = DEFAULT_ENV_FILE,
    *,
    rotate: bool = False,
    environ: Mapping[str, str] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> EnsureResult:
    """Resolve, persist, and return the local bearer tokens.

    Existing ``.env`` values win; a missing value is adopted from ``environ``
    when present, otherwise minted with ``token_factory``.  ``rotate`` mints
    every managed token regardless of existing values.  Derived variables are
    always mirrored from their source.
    """
    factory = _default_token if token_factory is None else token_factory
    environment = os.environ if environ is None else environ
    source_text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    existing = _read_values(source_text)

    values: dict[str, str] = {}
    dispositions: dict[str, str] = {}
    for name in MANAGED_TOKENS:
        current = None if rotate else existing.get(name) or environment.get(name)
        if current:
            values[name] = current
            dispositions[name] = "reused" if existing.get(name) else "adopted"
        else:
            values[name] = factory()
            dispositions[name] = "generated"
    for name, source in DERIVED_TOKENS:
        values[name] = values[source]
        dispositions[name] = "derived"

    env_file.write_text(_render(source_text, values), encoding="utf-8")
    return EnsureResult(values=values, dispositions=dispositions)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ensure local development bearer tokens exist in .env.",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Path to the environment file (default: repository .env).",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Mint every managed token instead of reusing existing values.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the per-variable disposition report.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the helper and report dispositions without printing values."""
    args = build_parser().parse_args(argv)
    env_file = Path(args.env_file)
    result = ensure_tokens(env_file, rotate=args.rotate)
    if not args.quiet:
        for name in (*MANAGED_TOKENS, *(name for name, _ in DERIVED_TOKENS)):
            print(f"{name}: {result.dispositions[name]}")
        print(f"env_file: {env_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
