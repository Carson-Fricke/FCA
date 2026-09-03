from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from harness.strategy import CALLBACK_ORDER
from thesis.artifacts.importer import (
    ArtifactHashMismatchError,
    ArtifactImporter,
    ArtifactImportError,
    HarnessArtifact,
    HarnessManifest,
    NativeGitBoundary,
)
from thesis.strategies import (
    StrategyBundle,
    StrategyValidationError,
    StrategyValidationRequest,
    StrategyValidationResult,
    handoff_to_sandbox,
)


def _bundle() -> StrategyBundle:
    return StrategyBundle.create(
        strategy_id="momentum",
        version="1.2.0",
        source={
            "strategy.py": "from harness.strategy import Strategy\r\n\nclass Momentum(Strategy):\r\n    pass\r\n",
        },
        tests={"test_strategy.py": "def test_strategy():\n    assert True\n"},
        dependencies=("numpy==2.1.0", "pandas==2.2.3"),
        platform_versions={"python": "3.12.6", "harness": "1.0.0"},
        harness_configuration={"seed": 7, "symbols": ["SPY", "AAPL"]},
        risk_declarations={"max_position": "100"},
        capability_declarations={"network": "disabled"},
        harness_manifest_reference="manifest-2025",
    )


def test_equivalent_source_config_and_dependencies_have_one_identity() -> None:
    first = _bundle()
    second = StrategyBundle.create(
        strategy_id="momentum",
        version="1.2.0",
        source={
            "strategy.py": "from harness.strategy import Strategy\n\nclass Momentum(Strategy):\n    pass\n"
        },
        tests={"test_strategy.py": b"def test_strategy():\n    assert True\n"},
        dependencies=("pandas==2.2.3", "numpy==2.1.0"),
        platform_versions={"harness": "1.0.0", "python": "3.12.6"},
        harness_configuration={"symbols": ["SPY", "AAPL"], "seed": 7},
        risk_declarations={"max_position": "100"},
        capability_declarations={"network": "disabled"},
        harness_manifest_reference="manifest-2025",
    )

    assert first.bundle_id == second.bundle_id
    assert first.canonical_bytes == second.canonical_bytes
    assert (
        first.source[0].content
        == b"from harness.strategy import Strategy\n\nclass Momentum(Strategy):\n    pass\n"
    )
    with pytest.raises(FrozenInstanceError):
        type(first).__setattr__(first, "version", "2.0.0")


def test_bundle_manifest_publishes_harness_strategy_contract() -> None:
    bundle = _bundle()

    manifest = bundle.manifest()

    assert manifest["bundleId"] == bundle.bundle_id
    assert bundle.harness_contract == "harness.strategy.contracts.Strategy"
    assert (
        manifest["canonicalBytesSha256"]
        == hashlib.sha256(bundle.canonical_bytes).hexdigest()
    )
    assert bundle.canonical_document["harnessContract"] == {
        "callbacks": list(CALLBACK_ORDER),
        "name": "harness.strategy.contracts.Strategy",
    }


class RecordingValidator:
    def __init__(self, result: StrategyValidationResult) -> None:
        self.result = result
        self.requests: list[StrategyValidationRequest] = []

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        self.requests.append(request)
        return self.result


def test_sandbox_handoff_uses_published_boundary_before_backtest() -> None:
    bundle = _bundle()
    validator = RecordingValidator(StrategyValidationResult(True, validation_id="v-1"))

    result = handoff_to_sandbox(bundle, validator)

    assert result.validation_id == "v-1"
    assert validator.requests
    request = validator.requests[0]
    assert request.bundle_sha256 == bundle.bundle_sha256
    assert request.harness_contract == "harness.strategy.contracts.Strategy"
    assert request.callback_order == CALLBACK_ORDER
    assert request.network_access == "disabled"
    assert request.source == bundle.source


def test_rejected_sandbox_handoff_prevents_launch() -> None:
    validator = RecordingValidator(
        StrategyValidationResult(False, ("syntax error", "forbidden import"))
    )

    with pytest.raises(StrategyValidationError, match="syntax error; forbidden import"):
        handoff_to_sandbox(_bundle(), validator)


def _manifest(payload: bytes = b"equity curve\n") -> HarnessManifest:
    return HarnessManifest(
        manifest_id="harness-run-1",
        strategy_version="1.2.0",
        experiment_id="experiment-42",
        workflow_execution_id="workflow-9",
        artifacts=(
            HarnessArtifact(
                name="metrics/equity.csv",
                uri="results/equity.csv",
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                media_type="text/csv",
            ),
        ),
    )


class RecordingGit:
    def __init__(self) -> None:
        self.commits: list[tuple[tuple[str, ...], str, str | None]] = []

    def head(self) -> str:
        return "a" * 40

    def commit(
        self,
        paths: Sequence[str],
        message: str,
        *,
        expected_head: str | None,
    ) -> str:
        self.commits.append((tuple(paths), message, expected_head))
        return "b" * 40


class MappingFilesystem:
    def __init__(self, files: dict[Path, bytes]) -> None:
        self.files = files

    def exists(self, path: Path) -> bool:
        return path in self.files

    def read_bytes(self, path: Path) -> bytes:
        return self.files[path]

    def mkdir(self, path: Path) -> None:
        pass

    def write_bytes(self, path: Path, content: bytes) -> None:
        self.files[path] = content


def test_import_hash_verifies_before_writing_anything(tmp_path: Path) -> None:
    payload = b"actual\n"
    manifest = _manifest(b"declared\n")
    filesystem = MappingFilesystem({tmp_path / "results/equity.csv": payload})

    with pytest.raises(ArtifactHashMismatchError):
        ArtifactImporter(
            tmp_path, filesystem=filesystem, git=RecordingGit()
        ).import_manifest(
            manifest,
            strategy_version="1.2.0",
            experiment_id="experiment-42",
            workflow_execution_id="workflow-9",
            source_root=tmp_path,
        )

    assert len(filesystem.files) == 1


def test_import_links_all_provenance_and_publishes_digest_paths(tmp_path: Path) -> None:
    payload = b"equity curve\n"
    source = tmp_path / "results/equity.csv"
    git = RecordingGit()
    filesystem = MappingFilesystem({source: payload})
    manifest = _manifest(payload)

    receipt = ArtifactImporter(
        tmp_path, filesystem=filesystem, git=git
    ).import_manifest(
        manifest,
        strategy_version="1.2.0",
        experiment_id="experiment-42",
        workflow_execution_id="workflow-9",
        source_root=tmp_path,
    )

    reference = receipt.references[0]
    expected_path = (
        f"backtests/experiment-42/artifacts/{hashlib.sha256(payload).hexdigest()}"
        "/metrics/equity.csv"
    )
    assert reference.uri == expected_path
    assert reference.artifact_id == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    assert reference.strategy_version == "1.2.0"
    assert reference.experiment_id == "experiment-42"
    assert reference.workflow_execution_id == "workflow-9"
    assert reference.harness_manifest_id == "harness-run-1"
    assert reference.harness_manifest_sha256 == manifest.sha256
    assert reference.git_commit == "b" * 40
    assert receipt.git_commit == "b" * 40
    assert filesystem.files[tmp_path / expected_path] == payload
    assert receipt.provenance_uri in git.commits[1][0]
    assert git.commits[0][2] == "a" * 40


def test_import_rejects_network_uris_without_network_access(tmp_path: Path) -> None:
    payload = b"equity curve\n"
    manifest = HarnessManifest(
        manifest_id="harness-run-1",
        artifacts=(
            HarnessArtifact(
                name="metrics/equity.csv",
                uri="https://results.invalid/equity.csv",
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
            ),
        ),
    )

    with pytest.raises(ArtifactImportError, match="network URIs"):
        ArtifactImporter(tmp_path, git=RecordingGit()).import_manifest(
            manifest,
            strategy_version="1.2.0",
            experiment_id="experiment-42",
            workflow_execution_id="workflow-9",
            source_root=tmp_path,
        )


def test_native_git_boundary_publishes_only_import_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            capture_output=True,
            check=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
        )
        return result.stdout.strip()

    git("init", "--quiet")
    git("config", "user.name", "FCA Test")
    git("config", "user.email", "test@example.invalid")
    (repository / "README.md").write_text("workspace\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "--quiet", "-m", "initial")
    imported = repository / "backtests/run/artifacts/file.txt"
    imported.parent.mkdir(parents=True)
    imported.write_bytes(b"artifact\n")

    parent = git("rev-parse", "HEAD")
    commit = NativeGitBoundary(repository).commit(
        ("backtests/run/artifacts/file.txt",),
        "import artifact",
        expected_head=parent,
    )

    assert commit == git("rev-parse", "HEAD")
    assert git("diff-tree", "--no-commit-id", "--name-only", "-r", commit) == (
        "backtests/run/artifacts/file.txt"
    )
