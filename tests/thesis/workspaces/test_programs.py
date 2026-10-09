"""Committed artifacts, content-fenced edits, and compiler input identity."""

import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from tests.thesis.workspaces._support import MemoryGitFactory, Repositories

from thesis.experiments.mandate import (
    DataWindowPolicy,
    LaunchPartition,
    derive_mandate,
    rfc3339,
)
from thesis.integrations.mcp import HarnessMcpTransportError, McpToolResult
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest
from thesis.workspaces.programs import (
    INDICATOR_TEMPLATE,
    MAX_NULL_BLOCK_SIZE,
    PROGRAM_TEMPLATES,
    STUDY_TEMPLATE,
    ProgramArtifactError,
    ProgramKind,
    ProgramPublicationError,
    WorkspacePrograms,
)

CONFIG: dict[str, object] = {
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-01-02T00:00:00Z",
    "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}],
}

DECLARATION: dict[str, object] = {
    "lookback": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
    "threshold": {"type": "number", "default": 0.5, "minimum": 0.0, "maximum": 1.0},
    "enabled": {"type": "boolean", "default": True},
    "mode": {
        "type": "string",
        "default": "fast",
        "levels": ["fast", "slow"],
        "description": "execution mode",
    },
}

DEFAULTS: dict[str, object] = {
    "lookback": 20,
    "threshold": 0.5,
    "enabled": True,
    "mode": "fast",
}


def _author_strategy(
    service: WorkspacePrograms,
    program_id: str = "alpha",
    declaration: Mapping[str, object] | None = DECLARATION,
) -> dict[str, object]:
    return service.author(
        "workspace-one",
        "strategy",
        program_id,
        PROGRAM_TEMPLATES["strategy"],
        configuration=CONFIG,
        parameter_declaration=declaration,
    )


def _run_points(
    harness: "Harness", tool: str = "run_backtest"
) -> list[tuple[str, Mapping[str, object]]]:
    return [call for call in harness.calls if call[0] == tool]


def _point_parameters(
    call: tuple[str, Mapping[str, object]],
) -> dict[str, object]:
    config = cast(dict[str, object], call[1]["config"])
    instances = cast(list[dict[str, object]], config["strategy_instances"])
    return cast(dict[str, object], instances[0]["parameters"])


class Harness:
    validation_identity: str | None = "compiler-one"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.during: Callable[[], object] | None = None
        self.reject = False

    def current_server_info(self) -> Mapping[str, object]:
        local = WorkspacePrograms(cast(LocalWorkspaceProvider, None))
        version, abi = local._sdk_metadata(local.sdk_root)
        return {
            "serverInfo": {
                "sdkIdentity": local._sdk_identity(),
                "sdkVersion": version,
                "sdkAbiVersion": abi,
                "validationIdentity": self.validation_identity,
            }
        }

    def read_sdk_reference(self, **arguments: object) -> McpToolResult:
        metadata = dict(
            cast(Mapping[str, object], self.current_server_info()["serverInfo"])
        )
        if "topic" in arguments:
            local = WorkspacePrograms(cast(LocalWorkspaceProvider, None))
            reference = local.read_sdk_reference(
                cast(str, arguments["topic"]),
                start_line=cast(int, arguments.get("start_line", 1)),
                end_line=cast(int | None, arguments.get("end_line")),
                max_chars=cast(int, arguments.get("max_chars", 16000)),
            )
            metadata.update(reference)
            metadata["content"] = metadata.pop("source")
        return McpToolResult(
            tool="read_sdk_reference", is_error=False, structured=metadata
        )

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> McpToolResult:
        self.calls.append((name, arguments or {}))
        if self.during:
            self.during()
        structured: dict[str, object] = {
            "valid": not self.reject,
            "diagnostics": [{"message": "check", "rendered": STUDY_TEMPLATE}],
        }
        config = (arguments or {}).get("config")
        job_id = config.get("job_id") if isinstance(config, Mapping) else None
        structured["job_id"] = job_id if isinstance(job_id, str) else "job-one"
        return McpToolResult(
            tool=name,
            is_error=self.reject,
            structured=structured,
        )


Programs = tuple[WorkspacePrograms, Repositories, Harness]
GitPrograms = tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness]


@pytest.fixture
def programs(
    tmp_path: Path,
) -> Programs:
    factory = MemoryGitFactory()
    provider = Repositories(tmp_path / "workspaces", factory)
    harness = Harness()
    return WorkspacePrograms(provider, harness, git_factory=factory), provider, harness


@pytest.fixture
def git_programs(
    tmp_path: Path,
) -> GitPrograms:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    template = load_workspace_manifest("config/thesis/workspace.yaml").model_dump(
        mode="json"
    )
    for workspace_id in ("workspace-one", "workspace-two"):
        data = {
            **template,
            "workspace_id": workspace_id,
            "repository_identity": {
                "provider": "local",
                "repository_id": workspace_id,
                "default_branch": "main",
            },
        }
        provider.create(WorkspaceManifest.model_validate(data))
    harness = Harness()
    return WorkspacePrograms(provider, harness), provider, harness


@pytest.mark.parametrize(
    "program_id", ["../escape", "A", "a/b", "a\\b", "C:foo", "a.rs", "", "a\n"]
)
def test_unsafe_ids_rejected(programs: Programs, program_id: str) -> None:
    with pytest.raises(ProgramArtifactError):
        programs[0].author("workspace-one", "study", program_id, STUDY_TEMPLATE)


@pytest.mark.slow
def test_targeted_updates_and_read_ranges(
    git_programs: GitPrograms,
) -> None:
    service, provider, _ = git_programs
    first = service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    version = cast(str, first["version"])
    with pytest.raises(ProgramArtifactError, match="exactly once"):
        service.update(
            "workspace-one",
            "study",
            "alpha",
            expected_version=version,
            expected="fn",
            replacement="x",
        )
    second = service.update(
        "workspace-one",
        "study",
        "alpha",
        expected_version=version,
        expected="self.frames += 1",
        replacement="self.frames += 2",
    )
    assert second["version"] != version
    with pytest.raises(ProgramArtifactError, match="stale"):
        service.update(
            "workspace-one",
            "study",
            "alpha",
            expected_version=version,
            expected="self.frames += 2",
            replacement="self.frames += 3",
        )
    read = service.read("workspace-one", "study", "alpha", start_line=3, end_line=5)
    assert read["source"] == "".join(STUDY_TEMPLATE.splitlines(keepends=True)[2:5])
    assert read["startLine"] == 3 and read["endLine"] == 5
    for start, end in [(0, 3), (4, 2), (1, 401), (1000, None)]:
        with pytest.raises(ProgramArtifactError, match="range"):
            service.read(
                "workspace-one", "study", "alpha", start_line=start, end_line=end
            )
    path = provider.resolve_path("workspace-one", "studies/alpha/program.rs")
    path.write_text("uncommitted edit", encoding="utf-8")
    assert "self.frames += 2" in cast(
        str, service.read("workspace-one", "study", "alpha")["source"]
    )
    with pytest.raises(ProgramArtifactError, match="uncommitted"):
        service.update(
            "workspace-one",
            "study",
            "alpha",
            expected_version=cast(str, second["version"]),
            expected="self.frames += 2",
            replacement="self.frames += 3",
        )


def test_listing_reports_committed_ids_versions_and_compact_configuration(
    programs: Programs,
) -> None:
    service, _, _ = programs
    first = service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    service.author(
        "workspace-one", "study", "beta", STUDY_TEMPLATE, configuration=CONFIG
    )
    service.author(
        "workspace-one",
        "indicator",
        "last",
        INDICATOR_TEMPLATE,
        configuration=CONFIG,
    )
    listing = service.list("workspace-one", "study")
    assert listing["workspaceId"] == "workspace-one"
    assert listing["kind"] == "study"
    assert listing["count"] == 2
    entries = cast(list[dict[str, object]], listing["programs"])
    assert [entry["programId"] for entry in entries] == ["alpha", "beta"]
    alpha = entries[0]
    assert alpha["artifactPath"] == "studies/alpha"
    assert alpha["version"] == first["version"]
    assert alpha["bundleSha256"] == first["version"]
    assert alpha["sourceSha256"] == first["sourceSha256"]
    assert alpha["gitCommit"] == listing["gitCommit"]
    assert alpha["interval"] == "1min"
    assert alpha["symbols"] == ["SPY"]
    assert alpha["indicatorIds"] == []
    assert alpha["parameters"] is None
    indicators = cast(
        list[dict[str, object]],
        service.list("workspace-one", "indicator")["programs"],
    )
    assert [entry["programId"] for entry in indicators] == ["last"]
    assert service.list("workspace-one", "study", prefix="be")["count"] == 1


def test_listing_bounds_empty_workspaces_and_unsafe_entries(
    programs: Programs,
) -> None:
    service, provider, _ = programs
    empty = service.list("workspace-two", "study")
    assert empty["programs"] == [] and empty["count"] == 0
    for program_id in ("alpha", "beta", "gamma", "delta"):
        service.author(
            "workspace-one", "study", program_id, STUDY_TEMPLATE, configuration=CONFIG
        )
    assert service.list("workspace-one", "study", limit=2)["count"] == 2
    assert service.list("workspace-one", "study", prefix="g")["count"] == 1
    for prefix in ("A", "", "a" * 65):
        with pytest.raises(ProgramArtifactError, match="prefix"):
            service.list("workspace-one", "study", prefix=prefix)
    for limit in (0, 201, True, 1.5):
        with pytest.raises(ProgramArtifactError, match="limit"):
            service.list("workspace-one", "study", limit=cast(int, limit))
    root = provider.open("workspace-one").root
    # A working-tree case collision makes the entry unreadable and it is skipped.
    # Case-insensitive filesystems cannot hold both spellings; skip there.
    collision = root / "studies" / "ALPHA"
    try:
        collision.mkdir()
    except FileExistsError:
        collision = None
    if collision is not None:
        listed = service.list("workspace-one", "study")
        assert [
            entry["programId"]
            for entry in cast(list[dict[str, object]], listed["programs"])
        ] == ["beta", "delta", "gamma"]
        collision.rmdir()
    # A symlinked artifact path is skipped rather than followed.
    try:
        shutil.rmtree(root / "studies" / "delta")
        (root / "studies" / "delta").symlink_to(
            root / "studies" / "alpha", target_is_directory=True
        )
    except OSError:
        pass
    else:
        linked = service.list("workspace-one", "study")
        assert "delta" not in [
            entry["programId"]
            for entry in cast(list[dict[str, object]], linked["programs"])
        ]


def test_validation_dependencies_and_unrelated_commits(
    programs: Programs,
) -> None:
    service, provider, harness = programs
    dep = service.author(
        "workspace-one", "indicator", "last", INDICATOR_TEMPLATE, configuration=CONFIG
    )
    service.author(
        "workspace-one",
        "study",
        "alpha",
        STUDY_TEMPLATE,
        indicator_ids=["last"],
        configuration={
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-02T00:00:00Z",
            "subscriptions": CONFIG["subscriptions"],
        },
    )
    receipt = service.validate("workspace-one", "study", "alpha")
    assert receipt["accepted"] is True
    assert "indicators/last/program.rs" in cast(dict[str, str], receipt["files"])
    assert STUDY_TEMPLATE not in str(receipt)
    service.author(
        "workspace-one", "study", "unrelated", STUDY_TEMPLATE, configuration=CONFIG
    )
    provider.resolve_path("workspace-one", "studies/alpha/program.rs").write_text(
        "dirty", encoding="utf-8"
    )
    assert service.run("workspace-one", "study", "alpha")["ok"] is True
    config = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert str(config["job_id"]).startswith("job-")
    assert config["shell"] == "study"
    instance = cast(list[dict[str, object]], config["strategy_instances"])[0]
    source = cast(dict[str, object], instance["parameters"])["source"]
    assert source == "mod r#last {\n" + INDICATOR_TEMPLATE + "\n}\n" + STUDY_TEMPLATE
    service.update(
        "workspace-one",
        "indicator",
        "last",
        expected_version=cast(str, dep["version"]),
        expected="bar.close",
        replacement="bar.open",
    )
    with pytest.raises(ProgramArtifactError, match="stale"):
        service.run("workspace-one", "study", "alpha")


def test_validation_race_and_rejections(
    programs: Programs,
) -> None:
    service, _, harness = programs
    receipt = service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    harness.during = lambda: service.update(
        "workspace-one",
        "study",
        "alpha",
        expected_version=cast(str, receipt["version"]),
        expected="self.frames += 1",
        replacement="self.frames += 2",
    )
    with pytest.raises(ProgramArtifactError, match="changed during validation"):
        service.validate("workspace-one", "study", "alpha")
    harness.during = None
    harness.reject = True
    assert service.validate("workspace-one", "study", "alpha")["accepted"] is False
    with pytest.raises(ProgramArtifactError, match="rejected"):
        service.run("workspace-one", "study", "alpha")


def test_indicator_wrapper_transitive_dependencies_and_cycles(
    programs: Programs,
) -> None:
    service, _, harness = programs
    first = service.author(
        "workspace-one", "indicator", "first", INDICATOR_TEMPLATE, configuration=CONFIG
    )
    service.author(
        "workspace-one",
        "indicator",
        "second",
        INDICATOR_TEMPLATE,
        indicator_ids=["first"],
        configuration={
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-02T00:00:00Z",
            "subscriptions": CONFIG["subscriptions"],
        },
    )
    service.validate("workspace-one", "indicator", "second")
    source = cast(dict[str, object], harness.calls[-1][1]["model"])["source"]
    assert "selected_indicator::AuthoredIndicator" in cast(str, source)
    assert "fn indicators(&mut self) -> Vec<&mut dyn Indicator>" in cast(str, source)
    assert "export_study_program!(IndicatorValidation)" in cast(str, source)
    with pytest.raises(ProgramArtifactError, match="cycle"):
        service.update(
            "workspace-one",
            "indicator",
            "first",
            expected_version=cast(str, first["version"]),
            expected="bar.close",
            replacement="bar.open",
            indicator_ids=["second"],
        )


def test_missing_harness_fails_closed(
    programs: Programs,
) -> None:
    _, provider, _ = programs
    service = WorkspacePrograms(provider, git_factory=provider.git_factory)
    service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    with pytest.raises(ProgramArtifactError, match="not configured"):
        service.validate("workspace-one", "study", "alpha")


def test_configuration_cannot_override_committed_source(
    programs: Programs,
) -> None:
    service = programs[0]
    for configuration in (
        {"source": "alternate"},
        {"parameters": {"source": "alternate"}},
        {"data": {"source_path": "../escape"}},
        {"config_path": "arbitrary"},
    ):
        with pytest.raises(ProgramArtifactError, match="override"):
            service.author(
                "workspace-one",
                "study",
                "alpha",
                STUDY_TEMPLATE,
                configuration=configuration,
            )


def test_strategy_dispatch_and_indicator_dependency_validation(
    programs: Programs,
) -> None:
    service, _, harness = programs
    service.author(
        "workspace-one",
        "strategy",
        "alpha",
        PROGRAM_TEMPLATES["strategy"],
        configuration=CONFIG,
    )
    assert service.validate("workspace-one", "strategy", "alpha")["accepted"] is True
    assert harness.calls[-1][0] == "validate_strategy"
    receipt = service.run("workspace-one", "strategy", "alpha")
    assert receipt["ok"] is True and receipt["parameterAssignment"] == {}
    assert harness.calls[-1][0] == "run_backtest"
    assert (
        cast(dict[str, object], harness.calls[-1][1]["config"])["shell"] == "backtest"
    )
    with pytest.raises(ProgramArtifactError, match="does not exist"):
        service.author(
            "workspace-one",
            "study",
            "missing",
            STUDY_TEMPLATE,
            indicator_ids=["unknown"],
        )


def test_missing_validation_and_nonaccepting_compiler_fail_closed(
    programs: Programs,
) -> None:
    service, _, harness = programs
    service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    with pytest.raises(ProgramArtifactError, match="requires validation"):
        service.run("workspace-one", "study", "alpha")

    class EmptyHarness(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            return McpToolResult(tool=name, is_error=False)

    service.harness = EmptyHarness()
    assert service.validate("workspace-one", "study", "alpha")["accepted"] is False
    service.harness = harness


def test_basic_template_defaults_follow_declared_inputs(
    programs: Programs,
) -> None:
    service, _, _ = programs
    config = {
        **CONFIG,
        "subscriptions": [{"data_type": "bars", "symbols": ["MSFT"], "interval": "1d"}],
    }
    for kind in ("study", "strategy", "indicator"):
        receipt = service.author("workspace-one", kind, "basic", configuration=config)
        source = cast(
            str,
            service.load("workspace-one", kind, "basic")["source"],
        )
        assert '"MSFT".into()' in source and '"1d".into()' in source
        assert "SPY" not in source and "1min" not in source
        assert "source" not in receipt
    with pytest.raises(ProgramArtifactError, match="not both"):
        service.author(
            "workspace-one",
            "study",
            "invalid",
            STUDY_TEMPLATE,
            template="basic",
            configuration=config,
        )


def test_configuration_only_update_and_stale_validation(
    programs: Programs,
) -> None:
    service, _, _ = programs
    first = service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    service.validate("workspace-one", "study", "alpha")
    new_config = {**CONFIG, "end": "2024-01-03T00:00:00Z"}
    second = service.update(
        "workspace-one",
        "study",
        "alpha",
        expected_version=cast(str, first["version"]),
        configuration=new_config,
    )
    assert (
        second["sourceSha256"] == first["sourceSha256"]
        and second["version"] != first["version"]
    )
    assert (
        service.load("workspace-one", "study", "alpha")["configuration"] == new_config
    )
    with pytest.raises(ProgramArtifactError, match="stale"):
        service.run("workspace-one", "study", "alpha")
    with pytest.raises(ProgramArtifactError, match="together"):
        service.update(
            "workspace-one",
            "study",
            "alpha",
            expected_version=cast(str, second["version"]),
            expected="frames",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"start": "2024-01-01T00:00:00"},
        {"end": "2023-01-01T00:00:00Z"},
        {"start": "2024-02-30T00:00:00Z"},
        {"subscriptions": []},
        {
            "subscriptions": [
                {"data_type": "bars", "symbols": ["MSFT"], "interval": "bad"}
            ]
        },
        {"symbols": ["MSFT"]},
        {"interval": "1d"},
        {"data": None},
        {"extra": float("nan")},
        {"extra": object()},
        {1: "non-string key"},
    ],
)
def test_invalid_configurations_rejected_before_commit(
    programs: Programs,
    change: dict[str, object],
) -> None:
    service, provider, _ = programs
    before = provider.store("workspace-one").head
    with pytest.raises(ProgramArtifactError):
        service.author(
            "workspace-one",
            "study",
            "alpha",
            STUDY_TEMPLATE,
            configuration={**CONFIG, **change},
        )
    assert provider.store("workspace-one").head == before


def test_sdk_root_deployment_and_version_mismatch(
    tmp_path: Path,
    programs: Programs,
) -> None:
    service, provider, harness = programs
    assert service.sdk_root.resolve() == Path("src/harness/strategy-sdk").resolve()
    missing = WorkspacePrograms(
        provider,
        harness,
        sdk_root=tmp_path / "not-deployed",
        git_factory=provider.git_factory,
    )
    with pytest.raises(ProgramArtifactError, match="supply sdk_root"):
        missing.read_sdk_reference("study")
    # A remote transport is still injected independently of local reference assets.
    service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    with pytest.raises(ProgramArtifactError, match="unavailable"):
        missing.load("workspace-one", "study", "alpha")
    with pytest.raises(ProgramArtifactError, match="unavailable"):
        missing.validate("workspace-one", "study", "alpha")
    assert not harness.calls
    deployed = tmp_path / "sdk"
    shutil.copytree(service.sdk_root, deployed)
    for crate in ("contracts", "num"):
        shutil.copytree(service.sdk_root.parent / crate, tmp_path / crate)
    matched = WorkspacePrograms(
        provider,
        harness,
        sdk_root=deployed,
        git_factory=provider.git_factory,
    )
    assert (
        matched.read_sdk_reference("consumer")["sdkIdentity"]
        == service.read_sdk_reference("consumer")["sdkIdentity"]
    )
    cargo = deployed / "Cargo.toml"
    cargo.write_text(
        cargo.read_text(encoding="utf-8").replace(
            'version = "0.1.0"', 'version = "9.0.0"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProgramArtifactError, match="do not match"):
        matched.read_sdk_reference("study")


def test_reads_bound_characters_not_only_lines(
    programs: Programs,
) -> None:
    service, _, _ = programs
    source = "// " + "x" * 20000 + "\n"
    service.author("workspace-one", "study", "long", source, configuration=CONFIG)
    result = service.read("workspace-one", "study", "long", max_chars=100)
    assert result["source"] == source[:100] and result["truncated"] is True
    reference = service.read_sdk_reference("program", max_chars=100)
    assert len(cast(str, reference["source"])) == 100 and reference["truncated"] is True


def test_configuration_diagnostics_are_actionable(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class Reject(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            return McpToolResult(
                tool=name,
                is_error=True,
                structured={"error": "subscription mismatch\nsource excerpt"},
            )

    service.harness = Reject()
    service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    receipt = service.validate("workspace-one", "study", "alpha")
    assert receipt["message"] == "subscription mismatch"
    assert receipt["executionCertainty"] == "observed_failure"
    assert "validate again" in cast(str, receipt["retryGuidance"])
    assert "source excerpt" not in str(receipt)


def test_publication_failure_retains_commit_and_explicit_recovery(
    programs: Programs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, provider, _ = programs
    publish = provider.publish

    def fail(workspace_id: str) -> str:
        raise RuntimeError("remote unavailable")

    monkeypatch.setattr(provider, "publish", fail)
    with pytest.raises(ProgramPublicationError) as error:
        service.author(
            "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
        )
    receipt = error.value.receipt
    assert (
        service.verify_completion("workspace-one", "study", "alpha", receipt)["source"]
        == STUDY_TEMPLATE
    )
    monkeypatch.setattr(provider, "publish", publish)
    assert (
        service.publish(
            "workspace-one", "study", "alpha", cast(str, receipt["gitCommit"])
        )
        == receipt
    )


def test_publish_leaves_missing_checkout_without_new_commit(
    programs: Programs,
) -> None:
    service, provider, _ = programs
    receipt = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    source = provider.resolve_path("workspace-one", "studies/alpha/program.rs")
    source.unlink()
    assert service.publish("workspace-one", "study", "alpha") == receipt
    assert not source.exists()
    assert provider.store("workspace-one").head == receipt["gitCommit"]


def test_sdk_dependency_change_invalidates_validation(
    tmp_path: Path,
    programs: Programs,
) -> None:
    service, provider, harness = programs
    for crate in ("strategy-sdk", "contracts", "num"):
        shutil.copytree(service.sdk_root.parent / crate, tmp_path / crate)
    deployed = WorkspacePrograms(
        provider,
        harness,
        sdk_root=tmp_path / "strategy-sdk",
        git_factory=provider.git_factory,
    )
    deployed.author("workspace-one", "study", "alpha", configuration=CONFIG)
    deployed.validate("workspace-one", "study", "alpha")
    dependency = tmp_path / "contracts/src/lib.rs"
    dependency.write_text(
        dependency.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8"
    )
    with pytest.raises(ProgramArtifactError, match="SDK identities"):
        deployed.run("workspace-one", "study", "alpha")


def test_latest_committed_source_configuration_and_both_symbol_scopes(
    programs: Programs,
) -> None:
    service, provider, harness = programs
    first = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    config = {
        **CONFIG,
        "subscriptions": [
            {"data_type": "bars", "symbols": ["MSFT", "AAPL"], "interval": "1d"},
            {"data_type": "quote", "symbols": ["MSFT"]},
        ],
        "data": {"requiredSymbols": ["MSFT", "AAPL"]},
    }
    updated_source = service.template_source("study", config)
    updated = service.update(
        "workspace-one",
        "study",
        "alpha",
        expected_version=cast(str, first["version"]),
        expected=service.template_source("study", CONFIG),
        replacement=updated_source,
        configuration=config,
    )
    validated = service.validate("workspace-one", "study", "alpha")
    provider.resolve_path("workspace-one", "studies/alpha/program.rs").write_text(
        "dirty", encoding="utf-8"
    )
    receipt = service.run("workspace-one", "study", "alpha")
    document = cast(dict[str, object], harness.calls[-1][1]["config"])
    instance = cast(list[dict[str, object]], document["strategy_instances"])[0]
    assert receipt["version"] == validated["version"] == updated["version"]
    assert (
        cast(dict[str, object], document["data"])["symbols"]
        == instance["symbols"]
        == ["AAPL", "MSFT"]
    )
    assert cast(dict[str, object], instance["parameters"])["source"] == updated_source
    assert document["data"] == {
        "symbols": ["AAPL", "MSFT"],
        "subscriptions": config["subscriptions"],
    }


def test_load_stored_validation_at_selected_commit(
    programs: Programs,
) -> None:
    service, provider, _ = programs
    authored = service.author(
        "workspace-one", "strategy", "alpha", configuration=CONFIG
    )
    assert service.load("workspace-one", "strategy", "alpha")["validation"] is None
    validation = service.validate("workspace-one", "strategy", "alpha")
    latest = service.load("workspace-one", "strategy", "alpha")
    assert latest["validation"] == validation
    assert validation["gitCommit"] == authored["gitCommit"]
    assert latest["gitCommit"] != validation["gitCommit"]
    assert latest["sdkIdentity"] == validation["sdkIdentity"]
    assert (
        service.load(
            "workspace-one", "strategy", "alpha", cast(str, authored["gitCommit"])
        )["validation"]
        is None
    )
    for key in (
        "workspaceId",
        "kind",
        "programId",
        "version",
        "sourceSha256",
        "bundleSha256",
        "files",
    ):
        assert validation[key] == latest[key]
    service.author("workspace-one", "study", "unrelated", configuration=CONFIG)
    assert (
        service.load("workspace-one", "strategy", "alpha")["validation"] == validation
    )
    path = provider.resolve_path("workspace-one", "strategies/alpha/validation.json")
    path.write_text('{"accepted":false}', encoding="utf-8")
    assert (
        service.load("workspace-one", "strategy", "alpha")["validation"] == validation
    )


@pytest.mark.parametrize("compiler", ["compiler-two", None])
def test_historical_load_reports_fresh_compiler_without_rewriting_validation(
    programs: Programs,
    compiler: str | None,
) -> None:
    service, provider, harness = programs
    authored = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    validated = service.validate("workspace-one", "study", "alpha")
    pinned = service.load("workspace-one", "study", "alpha")
    assert pinned["validationIdentity"] == "compiler-one"
    head = provider.store("workspace-one").head
    harness.validation_identity = compiler
    historical = service.load(
        "workspace-one", "study", "alpha", cast(str, pinned["gitCommit"])
    )
    assert historical["validationIdentity"] == compiler
    assert historical["validation"] == validated
    assert (
        cast(dict[str, object], historical["validation"])["validationIdentity"]
        == "compiler-one"
    )
    unvalidated = service.load(
        "workspace-one", "study", "alpha", cast(str, authored["gitCommit"])
    )
    assert unvalidated["validation"] is None
    assert unvalidated["validationIdentity"] == compiler
    with pytest.raises(ProgramArtifactError, match="compiler identity"):
        service.verify_completion("workspace-one", "study", "alpha", pinned)
    count = len(harness.calls)
    with pytest.raises(ProgramArtifactError, match="validation identity"):
        service.run("workspace-one", "study", "alpha")
    assert len(harness.calls) == count
    assert provider.store("workspace-one").head == head


def test_run_configuration_matches_native_authored_document_semantics(
    programs: Programs,
) -> None:
    service, _, harness = programs
    config = {
        **CONFIG,
        "requiredSymbols": ["SPY"],
        "required_symbols": ["SPY"],
        "data": {"snapshot_id": "pinned-data", "fingerprint": "b" * 64},
    }
    service.author("workspace-one", "study", "alpha", configuration=config)
    service.validate("workspace-one", "study", "alpha")
    validated = cast(dict[str, object], harness.calls[-1][1]["model"])
    receipt = service.run("workspace-one", "study", "alpha")
    assert receipt["parameterAssignment"] == {}
    document = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert document["start"] == CONFIG["start"] and document["end"] == CONFIG["end"]
    assert not {
        "subscriptions",
        "interval",
        "symbols",
        "requiredSymbols",
        "required_symbols",
    }.intersection(document)
    assert document["data"] == {
        "snapshot_id": "pinned-data",
        "fingerprint": "b" * 64,
        "symbols": ["SPY"],
        "subscriptions": CONFIG["subscriptions"],
    }
    assert cast(list[dict[str, object]], document["strategy_instances"])[0][
        "parameters"
    ] == {"source": validated["source"]}
    tick_config = {
        **CONFIG,
        "subscriptions": [{"data_type": "quote", "symbols": ["MSFT"]}],
    }
    service.author("workspace-one", "study", "ticks", configuration=tick_config)
    service.validate("workspace-one", "study", "ticks")
    service.run("workspace-one", "study", "ticks")
    tick_document = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert "bar_interval" not in cast(dict[str, object], tick_document["data"])
    assert cast(dict[str, object], tick_document["data"])["symbols"] == ["MSFT"]


def test_existing_provider_and_raw_client_injection(
    programs: Programs,
) -> None:
    from thesis.agent.harness_tools import HarnessToolProvider
    from thesis.integrations.mcp import HarnessMcpClient, HarnessMcpTransportError

    class Transport:
        fail = False

        def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
            if self.fail and request.get("method") == "tools/call":
                raise HarnessMcpTransportError(
                    "response lost", execution_certainty="unknown_outcome"
                )
            params = request.get("params")
            arguments = params.get("arguments") if isinstance(params, Mapping) else None
            config = arguments.get("config") if isinstance(arguments, Mapping) else None
            job_id = config.get("job_id") if isinstance(config, Mapping) else None
            structured: dict[str, object] = {"valid": True}
            if isinstance(job_id, str):
                structured["job_id"] = job_id
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "serverInfo": {
                        "name": "harness",
                        "version": "1",
                        **cast(
                            Mapping[str, object],
                            Harness().current_server_info()["serverInfo"],
                        ),
                    },
                    "isError": False,
                    "content": [],
                    "structuredContent": structured,
                },
            }

        def notify(self, request: Mapping[str, object]) -> None:
            pass

        def close(self) -> None:
            pass

    service, _, _ = programs
    transport = Transport()
    client = HarnessMcpClient(transport)
    provider = HarnessToolProvider(client)
    assert provider.client is client
    service.harness = client
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    assert service.validate("workspace-one", "study", "alpha")["accepted"] is True
    service.harness = provider
    assert service.validate("workspace-one", "study", "alpha")["accepted"] is True
    assert service.run("workspace-one", "study", "alpha")["ok"] is True
    transport.fail = True
    result = service.run("workspace-one", "study", "alpha")
    assert result["ok"] is False and result["executionCertainty"] == "unknown_outcome"
    assert "never automatically replay" in cast(str, result["retryGuidance"])


def test_job_ids_are_allocated_once_per_explicit_invocation(
    programs: Programs,
) -> None:
    service, _, harness = programs
    ids = []
    for workspace in ("workspace-one", "workspace-two"):
        service.author(workspace, "study", "alpha", configuration=CONFIG)
        service.validate(workspace, "study", "alpha")
        for _ in range(2):
            receipt = service.run(workspace, "study", "alpha")
            submitted = cast(dict[str, object], harness.calls[-1][1]["config"])[
                "job_id"
            ]
            assert isinstance(submitted, str)
            assert re.fullmatch(r"job-[0-9a-f]{32}", submitted)
            assert receipt["job_id"] == submitted
            assert "submittedRunId" not in receipt
            nested = cast(dict[str, object], receipt["result"])
            assert "job_id" not in nested and "run_id" not in nested
            ids.append(submitted)
    assert len(set(ids)) == 4


def test_authored_run_id_fails_harness_validation_instead_of_being_stripped(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class StrictHarness(Harness):
        """A harness whose strict configuration validation rejects ``run_id``."""

        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            config = (arguments or {}).get("config")
            if isinstance(config, Mapping) and "run_id" in config:
                self.calls.append((name, arguments or {}))
                return McpToolResult(
                    tool=name,
                    is_error=True,
                    structured={"error": "configuration.run_id is obsolete"},
                )
            return super().call_tool(name, arguments)

    harness = StrictHarness()
    service.harness = harness
    service.author(
        "workspace-one",
        "study",
        "alpha",
        STUDY_TEMPLATE,
        configuration={**CONFIG, "run_id": "legacy-fixed"},
    )
    assert service.validate("workspace-one", "study", "alpha")["accepted"] is True
    receipt = service.run("workspace-one", "study", "alpha")

    submitted = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert submitted["run_id"] == "legacy-fixed"
    assert receipt["ok"] is False
    assert receipt["executionCertainty"] == "unknown_outcome"
    assert "did not acknowledge" in cast(str, receipt["message"])


@pytest.mark.parametrize("identity", [None, "compiler-two"])
def test_remote_compiler_identity_changes_fail_closed_before_run(
    programs: Programs,
    identity: str | None,
) -> None:
    service, _, harness = programs
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    validated = service.validate("workspace-one", "study", "alpha")
    assert validated["validationIdentity"] == "compiler-one"
    harness.validation_identity = identity
    count = len(harness.calls)
    with pytest.raises(ProgramArtifactError, match="validation identity"):
        service.run("workspace-one", "study", "alpha")
    assert len(harness.calls) == count


def test_validation_without_compiler_identity_cannot_authorize_run(
    programs: Programs,
) -> None:
    service, _, harness = programs
    harness.validation_identity = None
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    receipt = service.validate("workspace-one", "study", "alpha")
    assert receipt["accepted"] is False
    assert "identity is unavailable" in cast(str, receipt["message"])


def test_raw_transport_unknown_run_outcome_retains_submitted_id(
    programs: Programs,
) -> None:
    from thesis.integrations.mcp import HarnessMcpTransportError

    class LostRun(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            if name == "run_study":
                self.calls.append((name, arguments or {}))
                raise HarnessMcpTransportError(
                    "response lost", execution_certainty="unknown_outcome"
                )
            return super().call_tool(name, arguments)

    service, _, _ = programs
    harness = LostRun()
    service.harness = harness
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    service.validate("workspace-one", "study", "alpha")
    receipt = service.run("workspace-one", "study", "alpha")
    submitted = cast(dict[str, object], harness.calls[-1][1]["config"])["job_id"]
    assert receipt["ok"] is False and receipt["executionCertainty"] == "unknown_outcome"
    assert receipt["job_id"] == submitted
    nested = cast(dict[str, object], receipt["result"])
    assert "job_id" not in nested and "run_id" not in nested
    assert "never automatically replay" in cast(str, receipt["retryGuidance"])


def test_fresh_public_facade_identity_blocks_remote_changes(
    programs: Programs,
) -> None:
    from thesis.agent.harness_tools import HarnessToolProvider
    from thesis.integrations.mcp import HarnessMcpClient, HarnessMcpTransportError

    class Transport:
        identity: str | None = "compiler-one"
        unavailable = False
        requests: list[Mapping[str, object]]

        def __init__(self) -> None:
            self.requests = []

        def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
            self.requests.append(request)
            if request["method"] == "initialize":
                if self.unavailable:
                    raise HarnessMcpTransportError("metadata unavailable")
                result = {
                    "serverInfo": {
                        **cast(
                            Mapping[str, object],
                            Harness().current_server_info()["serverInfo"],
                        ),
                        "validationIdentity": self.identity,
                    }
                }
            else:
                result = {
                    "isError": False,
                    "content": [],
                    "structuredContent": {
                        "valid": True,
                        "validationIdentity": "compiler-one",
                    },
                }
            return {"jsonrpc": "2.0", "id": request["id"], "result": result}

        def notify(self, request: Mapping[str, object]) -> None:
            pass

        def close(self) -> None:
            pass

    service, _, _ = programs
    transport = Transport()
    service.harness = HarnessToolProvider(HarnessMcpClient(transport))
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    assert service.validate("workspace-one", "study", "alpha")["accepted"] is True
    for identity in ("compiler-two", None):
        transport.identity = identity
        with pytest.raises(ProgramArtifactError, match="validation identity"):
            service.run("workspace-one", "study", "alpha")
        assert transport.requests[-1]["method"] == "initialize"
    transport.unavailable = True
    with pytest.raises(ProgramArtifactError, match="SDK metadata"):
        service.run("workspace-one", "study", "alpha")
    assert (
        len(
            [
                request
                for request in transport.requests
                if request["method"] == "tools/call"
            ]
        )
        == 1
    )


def test_compact_diagnostics_keep_repair_spans_and_children_without_excerpts(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class Diagnostics(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            return McpToolResult(
                tool=name,
                is_error=True,
                structured={
                    "error": "compile failed",
                    "diagnostics": [
                        {
                            "message": "missing method",
                            "level": "error",
                            "code": {
                                "code": "E0599",
                                "explanation": "large explanation",
                            },
                            "rendered": STUDY_TEMPLATE,
                            "spans": [
                                {
                                    "file_name": "src/lib.rs",
                                    "line_start": 12,
                                    "line_end": 12,
                                    "column_start": 4,
                                    "column_end": 8,
                                    "is_primary": True,
                                    "label": "not found",
                                    "text": [{"text": STUDY_TEMPLATE}],
                                    "expansion": {"source": STUDY_TEMPLATE},
                                }
                            ],
                            "children": [
                                {
                                    "message": "replace method",
                                    "level": "help",
                                    "rendered": STUDY_TEMPLATE,
                                    "spans": [
                                        {
                                            "line_start": 12,
                                            "column_start": 4,
                                            "suggested_replacement": "on_bar",
                                            "suggestion_applicability": (
                                                "MachineApplicable"
                                            ),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )

    service.harness = Diagnostics()
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    receipt = service.validate("workspace-one", "study", "alpha")
    detail = cast(list[dict[str, object]], receipt["diagnostics"])[0]
    assert detail["code"] == "E0599"
    span = cast(list[dict[str, object]], detail["spans"])[0]
    assert (
        span["line_start"] == 12
        and span["column_start"] == 4
        and span["label"] == "not found"
    )
    child = cast(list[dict[str, object]], detail["children"])[0]
    assert (
        cast(list[dict[str, object]], child["spans"])[0]["suggested_replacement"]
        == "on_bar"
    )
    assert STUDY_TEMPLATE not in str(receipt)
    assert "explanation" not in str(receipt) and "rendered" not in str(receipt)


def test_serving_sdk_mismatch_blocks_templates_validation_run_and_completion(
    programs: Programs,
) -> None:
    service, _, harness = programs
    authored = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    validation = service.validate("workspace-one", "study", "alpha")
    loaded = service.load("workspace-one", "study", "alpha")
    assert loaded["sdkIdentity"] == validation["sdkIdentity"]

    class Changed(Harness):
        def current_server_info(self) -> Mapping[str, object]:
            info = dict(
                cast(Mapping[str, object], super().current_server_info()["serverInfo"])
            )
            info["sdkIdentity"] = "f" * 64
            return {"serverInfo": info}

    changed = Changed()
    service.harness = changed
    for operation in (
        lambda: service.template_source("study", CONFIG),
        lambda: service.read_sdk_reference("study"),
        lambda: service.validate("workspace-one", "study", "alpha"),
        lambda: service.run("workspace-one", "study", "alpha"),
        lambda: service.load("workspace-one", "study", "alpha"),
        lambda: service.verify_completion("workspace-one", "study", "alpha", authored),
    ):
        with pytest.raises(ProgramArtifactError, match="SDK identities do not match"):
            operation()
    assert not changed.calls
    assert len(harness.calls) == 1


def test_sdk_reference_uses_exact_remote_source_and_metadata_fallback(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class Remote(Harness):
        reads: list[dict[str, object]]

        def __init__(self) -> None:
            super().__init__()
            self.reads = []

        def current_server_info(self) -> Mapping[str, object]:
            return {"serverInfo": {}}

        def read_sdk_reference(self, **arguments: object) -> McpToolResult:
            self.reads.append(arguments)
            metadata = dict(
                cast(
                    Mapping[str, object], Harness().current_server_info()["serverInfo"]
                )
            )
            metadata["sdkIdentity"] = "f" * 64
            if arguments:
                metadata.update(
                    topic=arguments["topic"],
                    content="remote exact source",
                    startLine=2,
                    endLine=3,
                    totalLines=10,
                    truncated=False,
                )
            return McpToolResult(
                tool="read_sdk_reference", is_error=False, structured=metadata
            )

    remote = Remote()
    service.harness = remote
    reference = service.read_sdk_reference("consumer", start_line=2, end_line=3)
    assert reference["source"] == "remote exact source"
    assert reference["sdkIdentity"] == "f" * 64
    assert remote.reads == [
        {},
        {"topic": "consumer", "start_line": 2, "end_line": 3, "max_chars": 16000},
    ]
    with pytest.raises(ProgramArtifactError, match="SDK identities"):
        service.read_sdk_reference("study")


def test_serving_sdk_reference_change_during_read_fails_closed(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class ChangedRead(Harness):
        def read_sdk_reference(self, **arguments: object) -> McpToolResult:
            result = super().read_sdk_reference(**arguments)
            return McpToolResult(
                tool=result.tool,
                is_error=False,
                structured={**result.structured, "sdkIdentity": "f" * 64},
            )

    service.harness = ChangedRead()
    with pytest.raises(ProgramArtifactError, match="unavailable or changed"):
        service.read_sdk_reference("consumer")


def test_sdk_change_during_validation_is_not_committed(
    programs: Programs,
) -> None:
    service, provider, harness = programs
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    before = provider.store("workspace-one").head
    harness.during = lambda: setattr(harness, "validation_identity", "compiler-two")
    with pytest.raises(
        ProgramArtifactError, match="compiler changed during validation"
    ):
        service.validate("workspace-one", "study", "alpha")
    assert provider.store("workspace-one").head == before


def test_local_sdk_guidance_and_artifact_reads_work_without_harness(
    programs: Programs,
) -> None:
    _, provider, _ = programs
    local = WorkspacePrograms(provider, git_factory=provider.git_factory)
    authored = local.author("workspace-one", "study", "alpha", configuration=CONFIG)
    assert local.read_sdk_reference("study")["source"] == STUDY_TEMPLATE
    assert (
        local.read("workspace-one", "study", "alpha")["version"] == authored["version"]
    )
    assert (
        local.load("workspace-one", "study", "alpha")["sdkIdentity"]
        == local._sdk_identity()
    )
    with pytest.raises(ProgramArtifactError, match="not configured"):
        local.validate("workspace-one", "study", "alpha")


def test_strategy_parameter_declaration_round_trips_in_load_and_list(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs
    _author_strategy(service)
    loaded = service.load("workspace-one", "strategy", "alpha")
    assert loaded["parameters"] == DECLARATION
    assert cast(dict[str, object], loaded["manifest"])["parameters"] == DECLARATION
    listing = service.list("workspace-one", "strategy")
    entry = cast(list[dict[str, object]], listing["programs"])[0]
    assert entry["parameters"] == DECLARATION
    service.author("workspace-one", "study", "plain", configuration=CONFIG)
    plain = service.load("workspace-one", "study", "plain")
    assert plain["parameters"] is None
    plain_entry = cast(
        list[dict[str, object]], service.list("workspace-one", "study")["programs"]
    )[0]
    assert plain_entry["parameters"] is None


@pytest.mark.parametrize(
    "declaration",
    [
        {"BadName": {"type": "integer", "default": 1}},
        {"1bad": {"type": "integer", "default": 1}},
        {"a" * 65: {"type": "integer", "default": 1}},
        {"source": {"type": "string", "default": "x"}},
        {"source_path": {"type": "string", "default": "x"}},
        {"symbols": {"type": "string", "default": "x"}},
        {"lookback_path": {"type": "integer", "default": 1}},
        {"lookbackPath": {"type": "integer", "default": 1}},
        {"lookback": 5},
        {"lookback": {"default": 20}},
        {"lookback": {"type": "float", "default": 20}},
        {"lookback": {"type": "integer"}},
        {"lookback": {"type": "integer", "default": True}},
        {"lookback": {"type": "integer", "default": 1.5}},
        {"lookback": {"type": "integer", "default": "20"}},
        {"ratio": {"type": "number", "default": "0.5"}},
        {"flag": {"type": "boolean", "default": 1}},
        {"mode": {"type": "string", "default": "medium", "levels": ["fast", "slow"]}},
        {"ratio": {"type": "number", "default": 0.5, "minimum": "low"}},
        {"ratio": {"type": "number", "default": 0.5, "minimum": 2.0, "maximum": 1.0}},
        {"ratio": {"type": "number", "default": 0.5, "minimum": float("nan")}},
        {"ratio": {"type": "number", "default": float("inf")}},
        {"mode": {"type": "string", "default": "fast", "minimum": 0}},
        {"lookback": {"type": "integer", "default": 1, "levels": ["a"]}},
        {"mode": {"type": "string", "default": "fast", "levels": []}},
        {"mode": {"type": "string", "default": "fast", "levels": ["fast", "fast"]}},
        {"mode": {"type": "string", "default": "fast", "levels": [1]}},
        {"lookback": {"type": "integer", "default": 1, "min": 0}},
        {"lookback": {"type": "integer", "default": 1, "description": 5}},
        5,
        ["lookback"],
    ],
)
def test_invalid_parameter_declarations_rejected_before_commit(
    programs: Programs,
    declaration: object,
) -> None:
    service, provider, _ = programs
    before = provider.store("workspace-one").head
    with pytest.raises(ProgramArtifactError):
        service.author(
            "workspace-one",
            "strategy",
            "alpha",
            PROGRAM_TEMPLATES["strategy"],
            configuration=CONFIG,
            parameter_declaration=cast(Mapping[str, object], declaration),
        )
    assert provider.store("workspace-one").head == before


def test_non_strategy_kinds_reject_nonempty_parameter_declarations(
    programs: Programs,
) -> None:
    service, provider, _ = programs
    before = provider.store("workspace-one").head
    for kind in ("study", "indicator"):
        with pytest.raises(ProgramArtifactError, match="only strateg"):
            service.author(
                "workspace-one",
                cast(ProgramKind, kind),
                "alpha",
                configuration=CONFIG,
                parameter_declaration=DECLARATION,
            )
    assert provider.store("workspace-one").head == before
    service.author(
        "workspace-one",
        "study",
        "plain",
        configuration=CONFIG,
        parameter_declaration={},
    )
    assert service.load("workspace-one", "study", "plain")["parameters"] is None


def test_parameter_declaration_update_clear_and_unchanged(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs
    first = _author_strategy(service)
    unchanged = service.update(
        "workspace-one",
        "strategy",
        "alpha",
        expected_version=cast(str, first["version"]),
        expected="struct AuthoredStrategy;",
        replacement="struct AuthoredStrategy { }",
    )
    assert (
        service.load("workspace-one", "strategy", "alpha")["parameters"] == DECLARATION
    )
    cleared = service.update(
        "workspace-one",
        "strategy",
        "alpha",
        expected_version=cast(str, unchanged["version"]),
        configuration={**CONFIG, "end": "2024-01-03T00:00:00Z"},
        parameter_declaration={},
    )
    loaded = service.load("workspace-one", "strategy", "alpha")
    assert loaded["parameters"] is None
    assert "parameters" not in cast(dict[str, object], loaded["manifest"])
    replacement = {"lookback": {"type": "integer", "default": 5}}
    restored = service.update(
        "workspace-one",
        "strategy",
        "alpha",
        expected_version=cast(str, cleared["version"]),
        configuration=CONFIG,
        parameter_declaration=replacement,
    )
    loaded = service.load("workspace-one", "strategy", "alpha")
    assert loaded["parameters"] == replacement
    assert cast(dict[str, object], loaded["manifest"])["parameters"] == replacement
    service.author("workspace-one", "study", "plain", configuration=CONFIG)
    study = service.load("workspace-one", "study", "plain")
    with pytest.raises(ProgramArtifactError, match="only strateg"):
        service.update(
            "workspace-one",
            "study",
            "plain",
            expected_version=cast(str, study["version"]),
            parameter_declaration=DECLARATION,
        )
    service.update(
        "workspace-one",
        "study",
        "plain",
        expected_version=cast(str, study["version"]),
        configuration={**CONFIG, "end": "2024-01-03T00:00:00Z"},
        parameter_declaration={},
    )
    assert service.load("workspace-one", "study", "plain")["parameters"] is None
    assert restored["version"] != first["version"]


def test_run_assignment_defaults_and_exact_instance_parameters(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    receipt = service.run("workspace-one", "strategy", "alpha")
    assert receipt["ok"] is True
    assert receipt["parameterAssignment"] == DEFAULTS
    source = cast(str, service.load("workspace-one", "strategy", "alpha")["source"])
    assert _point_parameters(harness.calls[-1]) == {"source": source, **DEFAULTS}
    explicit = service.run(
        "workspace-one",
        "strategy",
        "alpha",
        parameters={"lookback": 25.0, "mode": "slow"},
    )
    assert explicit["parameterAssignment"] == {
        **DEFAULTS,
        "lookback": 25,
        "mode": "slow",
    }
    assert cast(dict[str, object], explicit["parameterAssignment"])["lookback"] == 25
    service.author("workspace-one", "study", "plain", configuration=CONFIG)
    service.validate("workspace-one", "study", "plain")
    plain = service.run("workspace-one", "study", "plain")
    assert plain["parameterAssignment"] == {}
    plain_source = cast(str, service.load("workspace-one", "study", "plain")["source"])
    assert _point_parameters(harness.calls[-1]) == {"source": plain_source}


def test_run_assignment_rejections_before_submission(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    for parameters, message in (
        ({"lookback": "20"}, "integer"),
        ({"lookback": True}, "integer"),
        ({"lookback": 1.5}, "integer"),
        ({"threshold": True}, "number"),
        ({"threshold": "0.5"}, "number"),
        ({"threshold": float("nan")}, "finite"),
        ({"enabled": "yes"}, "true or false"),
        ({"mode": 5}, "string"),
        ({"mode": "medium"}, "one of"),
        ({"lookback": 0}, "at least"),
        ({"lookback": 201}, "at most"),
        ({"threshold": 1.5}, "at most"),
        ({"unknown": 1}, "unknown parameter"),
        ({"source": "override"}, "reserved"),
        ({"source_path": "x"}, "reserved"),
        ({"symbols": ["SPY"]}, "reserved"),
        ({"extra_path": "x"}, "reserved"),
        ({"extraPath": "x"}, "reserved"),
        ([("lookback", 20)], "assignment object"),
    ):
        count = len(harness.calls)
        with pytest.raises(ProgramArtifactError, match=message):
            service.run(
                "workspace-one",
                "strategy",
                "alpha",
                parameters=cast(Mapping[str, object], parameters),
            )
        assert len(harness.calls) == count
    service.author("workspace-one", "study", "plain", configuration=CONFIG)
    service.validate("workspace-one", "study", "plain")
    count = len(harness.calls)
    with pytest.raises(ProgramArtifactError, match="unknown parameter"):
        service.run("workspace-one", "study", "plain", parameters={"lookback": 5})
    assert len(harness.calls) == count


def test_run_batch_submits_sequentially_with_fresh_ids_and_stable_provenance(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    version = cast(str, service.load("workspace-one", "strategy", "alpha")["version"])
    submitted = [{"lookback": 10}, {"lookback": 30, "mode": "slow"}]
    result = service.run_batch("workspace-one", "strategy", "alpha", submitted)
    assert result["ok"] is True
    assert result["programId"] == "alpha" and result["version"] == version
    batch_id = cast(str, result["batchId"])
    assert batch_id.startswith("batch-alpha-")
    assert len(batch_id) == len("batch-alpha-") + 12
    points = cast(list[dict[str, object]], result["points"])
    assert [point["parameters"] for point in points] == [
        {**DEFAULTS, "lookback": 10},
        {**DEFAULTS, "lookback": 30, "mode": "slow"},
    ]
    job_ids = [
        cast(dict[str, object], call[1]["config"])["job_id"]
        for call in _run_points(harness)
    ]
    assert [point["jobId"] for point in points] == job_ids
    assert all(
        re.fullmatch(r"job-[0-9a-f]{32}", cast(str, job_id)) for job_id in job_ids
    )
    assert len(set(job_ids)) == 2
    assert all(point["state"] == "succeeded" for point in points)
    assert all("error" not in point for point in points)
    reversed_result = service.run_batch(
        "workspace-one", "strategy", "alpha", list(reversed(submitted))
    )
    assert reversed_result["batchId"] == batch_id
    reversed_points = cast(list[dict[str, object]], reversed_result["points"])
    reversed_ids = {cast(str, point["jobId"]) for point in reversed_points}
    assert len(reversed_ids) == 2
    assert reversed_ids.isdisjoint({cast(str, job_id) for job_id in job_ids})
    other = service.run_batch("workspace-one", "strategy", "alpha", [{"lookback": 99}])
    assert other["batchId"] != batch_id


def test_run_batch_pins_one_snapshot_for_every_point(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    mutated = {"done": False}

    def mutate() -> None:
        if mutated["done"] or not _run_points(harness):
            return
        mutated["done"] = True
        latest = service.load("workspace-one", "strategy", "alpha")
        service.update(
            "workspace-one",
            "strategy",
            "alpha",
            expected_version=cast(str, latest["version"]),
            expected="struct AuthoredStrategy;",
            replacement="struct AuthoredStrategy; // changed mid-batch",
        )

    harness.during = mutate
    result = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}, {"lookback": 30}],
    )
    assert result["ok"] is True
    run_calls = _run_points(harness)
    assert len(run_calls) == 2
    sources = [cast(str, _point_parameters(call)["source"]) for call in run_calls]
    assert sources == [sources[0]] * 2
    assert "changed mid-batch" not in sources[0]
    assert mutated["done"] is True


class _Refused(Harness):
    """Refuses every study launch before admission, with a definitive certainty."""

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> McpToolResult:
        if name == "run_study":
            self.calls.append((name, arguments or {}))
            raise HarnessMcpTransportError(
                "refused before admission", execution_certainty="not_started"
            )
        return super().call_tool(name, arguments)


def _ignore_settlement(_certainty: str, _message: str | None) -> None:
    """Retention fakes that only observe dispatch order ignore settlements."""


def test_run_batch_retains_every_job_before_its_dispatch(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    order: list[tuple[str, str]] = []

    def retain(
        job_id: str, tool_name: str, attribution: Mapping[str, object]
    ) -> Callable[[str, str | None], None]:
        order.append(("retain", job_id))
        assert tool_name == "run_backtest"
        assert attribution["programId"] == "alpha"
        assert attribution["kind"] == "strategy"
        assert attribution["batchKind"] == "batch"
        assert isinstance(attribution["batchId"], str)
        assert isinstance(attribution["parameterAssignmentSha256"], str)
        return _ignore_settlement

    harness.during = lambda: order.append(("dispatch", ""))
    result = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}, {"lookback": 30}],
        retain=retain,
    )
    assert result["ok"] is True
    assert [kind for kind, _ in order] == ["retain", "dispatch", "retain", "dispatch"]
    run_calls = _run_points(harness)
    assert [job_id for kind, job_id in order if kind == "retain"] == [
        cast(dict[str, object], call[1]["config"])["job_id"] for call in run_calls
    ]


def test_run_batch_retention_failure_never_dispatches_that_point(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    retained: list[str] = []

    def retain(
        job_id: str, tool_name: str, attribution: Mapping[str, object]
    ) -> Callable[[str, str | None], None]:
        retained.append(job_id)
        if len(retained) == 2:
            raise RuntimeError("tracking unavailable")
        return _ignore_settlement

    result = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}, {"lookback": 30}],
        retain=retain,
    )
    assert result["ok"] is False
    points = cast(list[dict[str, object]], result["points"])
    assert [point["state"] for point in points] == ["succeeded", "failed"]
    assert [point["executionCertainty"] for point in points] == [
        "observed_success",
        "not_started",
    ]
    assert points[1]["jobId"] == retained[1]
    error = cast(str, points[1]["error"])
    assert "retention failed" in error and "tracking unavailable" in error
    assert len(_run_points(harness)) == 1


def test_run_retention_failure_is_not_started_and_never_dispatches(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    retained: list[str] = []

    def retain(
        job_id: str, tool_name: str, attribution: Mapping[str, object]
    ) -> Callable[[str, str | None], None]:
        retained.append(job_id)
        raise RuntimeError("tracking unavailable")

    receipt = service.run("workspace-one", "strategy", "alpha", retain=retain)
    assert receipt["ok"] is False
    assert receipt["executionCertainty"] == "not_started"
    assert receipt["job_id"] == retained[0]
    assert "retention failed" in cast(str, receipt["message"])
    assert not _run_points(harness)


def test_dispatch_refused_before_admission_settles_the_job_as_not_started(
    programs: Programs,
) -> None:
    service, _, _ = programs
    service.harness = _Refused()
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    service.validate("workspace-one", "study", "alpha")
    settled: list[tuple[str, str | None]] = []

    def retain(
        job_id: str, tool_name: str, attribution: Mapping[str, object]
    ) -> Callable[[str, str | None], None]:
        return lambda certainty, message: settled.append((certainty, message))

    receipt = service.run("workspace-one", "study", "alpha", retain=retain)
    assert receipt["ok"] is False
    assert receipt["executionCertainty"] == "not_started"
    assert settled == [("not_started", "Harness program request failed.")]


def test_failure_after_dispatch_settles_as_unknown_never_not_started(
    programs: Programs,
) -> None:
    service, _, _ = programs
    service.harness = _Refused()
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    service.validate("workspace-one", "study", "alpha")

    def retain(
        job_id: str, tool_name: str, attribution: Mapping[str, object]
    ) -> Callable[[str, str | None], None]:
        def settle(_certainty: str, _message: str | None) -> None:
            raise OSError("tracking store unavailable")

        return settle

    receipt = service.run("workspace-one", "study", "alpha", retain=retain)
    assert receipt["ok"] is False
    assert receipt["executionCertainty"] == "unknown_outcome"
    assert "tracking store unavailable" in cast(str, receipt["message"])


def test_missing_job_acknowledgement_is_unknown_outcome(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class Silent(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            if name == "run_study":
                self.calls.append((name, arguments or {}))
                return McpToolResult(tool=name, is_error=False, structured={})
            return super().call_tool(name, arguments)

    harness = Silent()
    service.harness = harness
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    service.validate("workspace-one", "study", "alpha")
    receipt = service.run("workspace-one", "study", "alpha")
    submitted = cast(dict[str, object], harness.calls[-1][1]["config"])["job_id"]
    assert receipt["ok"] is False
    assert receipt["executionCertainty"] == "unknown_outcome"
    assert "did not acknowledge" in cast(str, receipt["message"])
    assert receipt["job_id"] == submitted


def test_mismatched_job_acknowledgement_is_unknown_outcome(
    programs: Programs,
) -> None:
    service, _, _ = programs

    class Wrong(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            if name == "run_study":
                self.calls.append((name, arguments or {}))
                return McpToolResult(
                    tool=name,
                    is_error=False,
                    structured={"job_id": "job-other", "state": "succeeded"},
                )
            return super().call_tool(name, arguments)

    harness = Wrong()
    service.harness = harness
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    service.validate("workspace-one", "study", "alpha")
    receipt = service.run("workspace-one", "study", "alpha")
    submitted = cast(dict[str, object], harness.calls[-1][1]["config"])["job_id"]
    assert receipt["ok"] is False
    assert receipt["executionCertainty"] == "unknown_outcome"
    assert "different job_id" in cast(str, receipt["message"])
    assert receipt["job_id"] == submitted
    assert submitted != "job-other"


def test_run_batch_validates_every_point_before_any_submission(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    count = len(harness.calls)
    for assignments, message in (
        ([{"lookback": 10}, {"unknown": 1}], "unknown parameter"),
        ([{"lookback": 10}, {"lookback": 10}], "duplicat"),
        ([{"lookback": 10}, {"lookback": 10.0}], "duplicat"),
        (
            [{"lookback": 10, "mode": "fast"}, {"mode": "fast", "lookback": 10}],
            "duplicat",
        ),
        ([], "at least one"),
        ("nope", "array"),
        ([{"lookback": 10}, "nope"], "assignment object"),
        ([{"lookback": 10}, {"lookback": 0}], "at least"),
    ):
        with pytest.raises(ProgramArtifactError, match=message):
            service.run_batch(
                "workspace-one",
                "strategy",
                "alpha",
                cast(Sequence[Mapping[str, object]], assignments),
            )
        assert len(harness.calls) == count
    with pytest.raises(ProgramArtifactError, match="indicators execute"):
        service.run_batch("workspace-one", "indicator", "alpha", [{"lookback": 10}])
    assert len(harness.calls) == count


def test_run_batch_records_unknown_points_without_replay(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs

    class Flaky(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            if (
                name == "run_backtest"
                and _point_parameters((name, arguments or {})).get("lookback") == 40
            ):
                self.calls.append((name, arguments or {}))
                raise HarnessMcpTransportError(
                    "response lost", execution_certainty="unknown_outcome"
                )
            return super().call_tool(name, arguments)

    harness = Flaky()
    service.harness = harness
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    result = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 20}, {"lookback": 40}, {"lookback": 60}],
    )
    points = cast(list[dict[str, object]], result["points"])
    assert result["ok"] is False
    assert [point["state"] for point in points] == ["succeeded", "unknown", "succeeded"]
    unknown = points[1]
    assert isinstance(unknown["jobId"], str)
    assert "Harness program request failed" in cast(str, unknown["error"])
    assert len(_run_points(harness)) == 3
    harness.reject = True
    failed = service.run_batch("workspace-one", "strategy", "alpha", [{"lookback": 20}])
    failed_points = cast(list[dict[str, object]], failed["points"])
    assert failed["ok"] is False and failed_points[0]["state"] == "failed"


def test_configuration_guard_still_rejects_nested_parameters(
    programs: Programs,
) -> None:
    service, provider, _ = programs
    before = provider.store("workspace-one").head
    for configuration in (
        {"parameters": {"source": "alternate"}},
        {"data": {"parameters": {"lookback": 1}}},
        {"data": {"nested": [{"parameters": {}}]}},
    ):
        with pytest.raises(ProgramArtifactError, match="override"):
            service.author(
                "workspace-one",
                "strategy",
                "guard",
                PROGRAM_TEMPLATES["strategy"],
                configuration={**CONFIG, **configuration},
                parameter_declaration=DECLARATION,
            )
    assert provider.store("workspace-one").head == before


def _launch_request(cutoff: datetime) -> dict[str, object]:
    """The launch request persisted for a cutoff with the default partition."""
    return {
        "createdAt": cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "partition": LaunchPartition(1825, 90, 1, 0).as_dict(),
    }


def _policy(cutoff: datetime) -> DataWindowPolicy:
    mandate = derive_mandate("workspace-one", "execution-1", _launch_request(cutoff))
    assert mandate is not None
    return mandate.default_window


def _forward_policy(cutoff: datetime) -> DataWindowPolicy:
    mandate = derive_mandate("workspace-one", "execution-1", _launch_request(cutoff))
    assert mandate is not None
    return mandate.forward_window


def test_run_enforces_research_mandate_window(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    count = len(harness.calls)
    allowed = _policy(datetime(2025, 6, 1, tzinfo=UTC))
    assert (
        service.run("workspace-one", "strategy", "alpha", data_window=allowed)["ok"]
        is True
    )
    assert len(_run_points(harness)) == 1
    rejected = _policy(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ProgramArtifactError) as error:
        service.run("workspace-one", "strategy", "alpha", data_window=rejected)
    assert "at or before 2023-10-02T00:00:00Z (exclusive)" in str(error.value)
    assert "allowed development window" in str(error.value)
    assert len(harness.calls) == count + 1
    assert len(_run_points(harness)) == 1


def test_run_batch_fails_fast_before_any_submission_when_window_crosses(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    allowed = _policy(datetime(2025, 6, 1, tzinfo=UTC))
    result = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}, {"lookback": 30}],
        data_window=allowed,
    )
    assert result["ok"] is True
    submitted = len(_run_points(harness))
    assert submitted == 2
    rejected = _policy(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(
        ProgramArtifactError, match="outside the allowed development window"
    ):
        service.run_batch(
            "workspace-one",
            "strategy",
            "alpha",
            [{"lookback": 10}],
            data_window=rejected,
        )
    assert len(_run_points(harness)) == submitted


def test_batch_identities_include_the_effective_window(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    baseline = service.run_batch(
        "workspace-one", "strategy", "alpha", [{"lookback": 10}]
    )
    development = _policy(datetime(2025, 6, 1, tzinfo=UTC))
    same_window = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}],
        data_window=development,
    )
    assert same_window["batchId"] == baseline["batchId"]
    forward = _forward_policy(datetime(2025, 6, 1, tzinfo=UTC))
    overridden = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}],
        data_window=forward,
    )
    assert overridden["batchId"] != baseline["batchId"]
    null_baseline = service.run_null_batch(
        "workspace-one", "strategy", "alpha", block_size=5, seeds=[1]
    )
    null_overridden = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        block_size=5,
        seeds=[1],
        data_window=forward,
    )
    assert null_overridden["batchId"] != null_baseline["batchId"]


def test_run_and_batch_stay_unrestricted_without_a_policy(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    assert service.run("workspace-one", "strategy", "alpha")["ok"] is True
    result = service.run_batch("workspace-one", "strategy", "alpha", [{"lookback": 10}])
    assert result["ok"] is True
    assert len(_run_points(harness)) == 2


def test_forward_override_policy_rewrites_start_and_end_on_run(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    policy = _forward_policy(datetime(2025, 6, 1, tzinfo=UTC))
    assert policy.override is True
    receipt = service.run("workspace-one", "strategy", "alpha", data_window=policy)
    assert receipt["ok"] is True
    document = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert document["start"] == rfc3339(policy.start_ns)
    assert document["end"] == rfc3339(policy.end_ns)
    assert document["start"] != CONFIG["start"] and document["end"] != CONFIG["end"]


def test_development_policy_keeps_the_committed_window_on_run(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    policy = _policy(datetime(2025, 6, 1, tzinfo=UTC))
    assert policy.override is False
    receipt = service.run("workspace-one", "strategy", "alpha", data_window=policy)
    assert receipt["ok"] is True
    document = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert document["start"] == CONFIG["start"]
    assert document["end"] == CONFIG["end"]


def test_forward_override_policy_rewrites_every_batch_and_null_point(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    policy = _forward_policy(datetime(2025, 6, 1, tzinfo=UTC))
    batch = service.run_batch(
        "workspace-one",
        "strategy",
        "alpha",
        [{"lookback": 10}, {"lookback": 30}],
        data_window=policy,
    )
    assert batch["ok"] is True
    assert len(_run_points(harness)) == 2
    null_batch = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        {"lookback": 10},
        block_size=5,
        seeds=[1, 2],
        data_window=policy,
    )
    assert null_batch["ok"] is True
    run_calls = _run_points(harness)
    assert len(run_calls) == 4
    for call in run_calls:
        document = cast(dict[str, object], call[1]["config"])
        assert document["start"] == rfc3339(policy.start_ns)
        assert document["end"] == rfc3339(policy.end_ns)


def _null_config(
    call: tuple[str, Mapping[str, object]],
) -> dict[str, object]:
    config = cast(dict[str, object], call[1]["config"])
    return cast(dict[str, object], config["null"])


def test_run_null_batch_uses_fresh_opaque_ids_with_stable_provenance(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    version = cast(str, service.load("workspace-one", "strategy", "alpha")["version"])
    result = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        {"lookback": 10},
        block_size=7,
        seeds=[3, 1, 2],
    )
    assert result["ok"] is True
    assert result["programId"] == "alpha" and result["version"] == version
    batch_id = cast(str, result["batchId"])
    assert batch_id.startswith("null-alpha-")
    assert len(batch_id) == len("null-alpha-") + 12
    points = cast(list[dict[str, object]], result["points"])
    assert [point["seed"] for point in points] == [3, 1, 2]
    assert [point["state"] for point in points] == ["succeeded"] * 3
    assert all("error" not in point for point in points)
    for point, expected in zip(points, (3, 1, 2), strict=True):
        assert point["parameters"] == {**DEFAULTS, "lookback": 10}
        assert re.fullmatch(r"job-[0-9a-f]{32}", cast(str, point["jobId"]))
        assert not cast(str, point["jobId"]).endswith(f"null-{expected}")
    run_calls = _run_points(harness)
    assert len(run_calls) == 3
    assert [cast(str, point["jobId"]) for point in points] == [
        cast(dict[str, object], call[1]["config"])["job_id"] for call in run_calls
    ]
    assert len({cast(str, point["jobId"]) for point in points}) == 3
    for call, expected in zip(run_calls, (3, 1, 2), strict=True):
        assert _null_config(call) == {
            "mode": "block_permutation",
            "block_size": 7,
            "seed": expected,
        }
    reordered = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        {"lookback": 10},
        block_size=7,
        seeds=[2, 3, 1],
    )
    assert reordered["batchId"] == batch_id
    reordered_points = cast(list[dict[str, object]], reordered["points"])
    reordered_ids = [cast(str, point["jobId"]) for point in reordered_points]
    assert [point["seed"] for point in reordered_points] == [2, 3, 1]
    assert len(set(reordered_ids)) == 3
    assert set(reordered_ids).isdisjoint(
        {cast(str, point["jobId"]) for point in points}
    )
    changed_block = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        {"lookback": 10},
        block_size=8,
        seeds=[1, 2, 3],
    )
    assert changed_block["batchId"] != batch_id
    assert {
        cast(str, point["jobId"])
        for point in cast(list[dict[str, object]], changed_block["points"])
    }.isdisjoint({cast(str, point["jobId"]) for point in points})
    changed_parameters = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        {"lookback": 11},
        block_size=7,
        seeds=[1, 2, 3],
    )
    assert changed_parameters["batchId"] != batch_id


def test_run_null_batch_validates_before_any_submission(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    assert MAX_NULL_BLOCK_SIZE == 10_000
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    count = len(harness.calls)
    for block_size, seeds, message in (
        (0, [1], "1 to 10000"),
        (10001, [1], "1 to 10000"),
        (True, [1], "1 to 10000"),
        (1.5, [1], "1 to 10000"),
        (5, [], "at least one"),
        (5, [1, 1], "unique"),
        (5, [-1], "at least 0"),
        (5, [1.5], "integer"),
        (5, [True], "integer"),
        (5, "1", "array"),
    ):
        with pytest.raises(ProgramArtifactError, match=message):
            service.run_null_batch(
                "workspace-one",
                "strategy",
                "alpha",
                block_size=cast(int, block_size),
                seeds=cast(Sequence[int], seeds),
            )
        assert len(harness.calls) == count
    with pytest.raises(ProgramArtifactError, match="unknown parameter"):
        service.run_null_batch(
            "workspace-one",
            "strategy",
            "alpha",
            {"unknown": 1},
            block_size=5,
            seeds=[1],
        )
    assert len(harness.calls) == count
    with pytest.raises(ProgramArtifactError, match="indicators execute"):
        service.run_null_batch(
            "workspace-one", "indicator", "alpha", block_size=5, seeds=[1]
        )
    assert len(harness.calls) == count


def test_run_null_batch_records_unknown_points_without_replay(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs

    class Flaky(Harness):
        def call_tool(
            self, name: str, arguments: Mapping[str, object] | None = None
        ) -> McpToolResult:
            call = (name, arguments or {})
            if name == "run_backtest" and _null_config(call)["seed"] == 2:
                self.calls.append(call)
                raise HarnessMcpTransportError(
                    "response lost", execution_certainty="unknown_outcome"
                )
            return super().call_tool(name, arguments)

    harness = Flaky()
    service.harness = harness
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    result = service.run_null_batch(
        "workspace-one", "strategy", "alpha", block_size=5, seeds=[1, 2, 3]
    )
    points = cast(list[dict[str, object]], result["points"])
    assert result["ok"] is False
    assert [point["state"] for point in points] == [
        "succeeded",
        "unknown",
        "succeeded",
    ]
    unknown = points[1]
    assert isinstance(unknown["jobId"], str)
    assert "Harness program request failed" in cast(str, unknown["error"])
    assert len(_run_points(harness)) == 3
    harness.reject = True
    failed = service.run_null_batch(
        "workspace-one", "strategy", "alpha", block_size=5, seeds=[1]
    )
    failed_points = cast(list[dict[str, object]], failed["points"])
    assert failed["ok"] is False and failed_points[0]["state"] == "failed"


def test_run_null_batch_fails_fast_before_any_submission_when_window_crosses(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    rejected = _policy(datetime(2024, 1, 1, tzinfo=UTC))
    count = len(harness.calls)
    with pytest.raises(
        ProgramArtifactError, match="outside the allowed development window"
    ):
        service.run_null_batch(
            "workspace-one",
            "strategy",
            "alpha",
            block_size=5,
            seeds=[1, 2],
            data_window=rejected,
        )
    assert len(harness.calls) == count
    allowed = _policy(datetime(2025, 6, 1, tzinfo=UTC))
    result = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        block_size=5,
        seeds=[1, 2],
        data_window=allowed,
    )
    assert result["ok"] is True
    assert len(_run_points(harness)) == 2


def test_large_null_design_is_submitted_in_explicit_bounded_chunks(
    programs: Programs,
) -> None:
    service, _, harness = programs
    _author_strategy(service)
    service.validate("workspace-one", "strategy", "alpha")
    seeds = list(range(70))
    first = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        block_size=1,
        seeds=seeds,
        mode="permutation",
        chunk_size=40,
    )
    second = service.run_null_batch(
        "workspace-one",
        "strategy",
        "alpha",
        block_size=1,
        seeds=seeds,
        mode="permutation",
        offset=40,
        chunk_size=40,
    )
    assert first["batchId"] == second["batchId"]
    assert first["completedSubmission"] is False
    assert first["nextOffset"] == 40
    assert second["completedSubmission"] is True
    assert second["nextOffset"] == 70
    assert len(_run_points(harness)) == 70
    points = cast(list[dict[str, object]], first["points"]) + cast(
        list[dict[str, object]], second["points"]
    )
    assert [point["seed"] for point in points] == seeds
    assert len({point["jobId"] for point in points}) == 70
    assert all(
        _null_config(call)["mode"] == "permutation" for call in _run_points(harness)
    )
