"""Focused tests for the local bearer-token helper.

Fixed token factories only: no randomness, no environment, no network.  The
helper is loaded by file path because ``scripts/local`` is a script directory,
not an installed package.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "local" / "dev_tokens.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("dev_tokens", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dev_tokens = _load_module()


def _factory(values: Iterable[str]) -> Callable[[], str]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


def test_generates_all_missing_tokens_and_derives_public(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    result = dev_tokens.ensure_tokens(
        env_file,
        environ={},
        token_factory=_factory(["lake", "harness", "thesis"]),
    )

    assert result.dispositions == {
        "LAKE_API__BEARER_TOKEN": "generated",
        "HARNESS_API_BEARER_TOKEN": "generated",
        "THESIS_API__BEARER_TOKEN": "generated",
        "PUBLIC_THESIS_API_BEARER_TOKEN": "derived",
    }
    assert (
        result.values["PUBLIC_THESIS_API_BEARER_TOKEN"]
        == result.values["THESIS_API__BEARER_TOKEN"]
    )
    text = env_file.read_text(encoding="utf-8")
    for name in (
        "LAKE_API__BEARER_TOKEN",
        "HARNESS_API_BEARER_TOKEN",
        "THESIS_API__BEARER_TOKEN",
        "PUBLIC_THESIS_API_BEARER_TOKEN",
    ):
        assert f"{name}=" in text


def test_reuses_existing_values_and_preserves_unrelated_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LAKE_PROVIDER__API_KEY=provider-key\n"
        "# a note\n"
        "THESIS_API__BEARER_TOKEN=keep-thesis\n",
        encoding="utf-8",
    )

    result = dev_tokens.ensure_tokens(
        env_file,
        environ={},
        token_factory=_factory(["new-lake", "new-harness"]),
    )

    assert result.dispositions["THESIS_API__BEARER_TOKEN"] == "reused"
    assert result.values["THESIS_API__BEARER_TOKEN"] == "keep-thesis"
    assert result.values["PUBLIC_THESIS_API_BEARER_TOKEN"] == "keep-thesis"
    text = env_file.read_text(encoding="utf-8")
    assert "LAKE_PROVIDER__API_KEY=provider-key" in text
    assert "# a note" in text
    assert text.count("THESIS_API__BEARER_TOKEN=") == 1
    assert text.count("PUBLIC_THESIS_API_BEARER_TOKEN=") == 1


def test_adopts_missing_value_from_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    result = dev_tokens.ensure_tokens(
        env_file,
        environ={"HARNESS_API_BEARER_TOKEN": "from-env"},
        token_factory=_factory(["lake", "thesis"]),
    )

    assert result.dispositions["HARNESS_API_BEARER_TOKEN"] == "adopted"
    assert result.values["HARNESS_API_BEARER_TOKEN"] == "from-env"


def test_rotate_mints_every_managed_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HARNESS_API_BEARER_TOKEN=old\nTHESIS_API__BEARER_TOKEN=old\n",
        encoding="utf-8",
    )

    result = dev_tokens.ensure_tokens(
        env_file,
        rotate=True,
        environ={},
        token_factory=_factory(["r-lake", "r-harness", "r-thesis"]),
    )

    assert set(result.values.values()) == {"r-lake", "r-harness", "r-thesis"}
    assert result.dispositions["PUBLIC_THESIS_API_BEARER_TOKEN"] == "derived"
    assert result.values["PUBLIC_THESIS_API_BEARER_TOKEN"] == "r-thesis"


def test_is_idempotent_across_runs(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    first = dev_tokens.ensure_tokens(
        env_file,
        environ={},
        token_factory=_factory(["lake", "harness", "thesis"]),
    )

    second = dev_tokens.ensure_tokens(
        env_file,
        environ={},
        token_factory=_factory([]),
    )

    assert second.values == first.values
    assert second.dispositions["LAKE_API__BEARER_TOKEN"] == "reused"
    text = env_file.read_text(encoding="utf-8")
    assert text.count("LAKE_API__BEARER_TOKEN=") == 1
    assert text.count("# Local development bearer tokens.") == 1
