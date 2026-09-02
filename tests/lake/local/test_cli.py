import json
from pathlib import Path

import pytest

from lake.cli import local


def test_documented_smoke_command_is_stable() -> None:
    repository = Path(__file__).resolve().parents[3]
    documentation = (repository / "docs" / "local-market-lake.md").read_text(
        encoding="utf-8"
    )
    instructions = (
        repository / ".github" / "instructions" / "market-lake-local.instructions.md"
    ).read_text(encoding="utf-8")
    command = "uv run lake local ingest --symbol AAPL --max-records 100"

    assert command in documentation
    assert command in instructions


def test_api_command_requires_token_before_starting_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    started = False

    def run(*args: object, **kwargs: object) -> None:
        nonlocal started
        del args, kwargs
        started = True

    monkeypatch.setattr(local.uvicorn, "run", run)

    result = local.main(["api", "--data-dir", str(tmp_path / "local")], environ={})

    assert result == 2
    assert started is False
    assert "LAKE_API__BEARER_TOKEN is required" in capsys.readouterr().err


def test_api_command_never_attempts_azure_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    started: dict[str, object] = {}

    def forbidden_credential(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("local mode attempted Azure authentication")

    def run(application: object, *, host: str, port: int) -> None:
        started.update(application=application, host=host, port=port)

    monkeypatch.setattr("lake.cli.ingest.DefaultAzureCredential", forbidden_credential)
    monkeypatch.setattr(local.uvicorn, "run", run)

    result = local.main(
        ["api", "--data-dir", str(tmp_path / "local")],
        environ={"LAKE_API__BEARER_TOKEN": "local-token"},
    )

    assert result == 0
    assert started["host"] == "127.0.0.1"
    assert started["port"] == 8000


def test_ingest_command_passes_the_documented_bound_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    received: dict[str, object] = {}

    async def ingest(**kwargs: object) -> dict[str, object]:
        received.update(kwargs)
        return {"bounded_records_per_data_type": kwargs["max_records"], "ok": True}

    monkeypatch.setattr(local, "run_local_ingest", ingest)

    result = local.main(
        [
            "ingest",
            "--symbol",
            "AAPL",
            "--max-records",
            "100",
            "--data-dir",
            str(tmp_path / "local"),
        ],
        environ={
            "LAKE_PROVIDER__API_KEY": "fake-key",
            "LAKE_PROVIDER__API_SECRET": "fake-secret",
        },
    )

    assert result == 0
    assert received["symbol"] == "AAPL"
    assert received["max_records"] == 100
    assert json.loads(capsys.readouterr().out) == {
        "bounded_records_per_data_type": 100,
        "ok": True,
    }


def test_reset_requires_confirmation_and_removes_only_local_state(
    tmp_path, capsys
) -> None:
    data_dir = tmp_path / "local"
    layout = local.LocalLayout(data_dir)
    layout.initialize()

    refused = local.main(["reset", "--data-dir", str(data_dir)], environ={})
    removed = local.main(["reset", "--yes", "--data-dir", str(data_dir)], environ={})

    output = capsys.readouterr()
    assert refused == 2
    assert "reset requires --yes" in output.err
    assert removed == 0
    assert json.loads(output.out)["removed"] is True
    assert not data_dir.exists()
