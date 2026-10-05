"""Committed artifacts, content-fenced edits, and compiler input identity."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

from thesis.integrations.mcp import McpToolResult
from thesis.persistence.git import GitCoordinator
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest
from thesis.workspaces.programs import (
    INDICATOR_TEMPLATE,
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
        return McpToolResult(
            tool=name,
            is_error=self.reject,
            structured={
                "job_id": "job-one",
                "valid": not self.reject,
                "diagnostics": [{"message": "check", "rendered": STUDY_TEMPLATE}],
            },
        )


@pytest.fixture
def programs(
    tmp_path: Path,
) -> tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness]:
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


def test_commits_only_artifacts_and_preserves_index(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, _ = programs
    root = provider.open("workspace-one").root
    git = GitCoordinator(root)
    (root / "unrelated.txt").write_text("staged", encoding="utf-8")
    git._run("add", "--", "unrelated.txt")
    before = git.head()
    receipt = service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    assert receipt["gitCommit"] != before
    assert "source" not in receipt
    assert set(
        git._run(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            cast(str, receipt["gitCommit"]),
        ).stdout.splitlines()
    ) == {"studies/alpha/program.rs", "studies/alpha/manifest.json"}
    assert git._run("diff", "--cached", "--name-only").stdout.strip() == "unrelated.txt"
    assert service.read("workspace-one", "study", "alpha")["source"] == STUDY_TEMPLATE
    with pytest.raises(ProgramArtifactError, match="does not exist"):
        service.read("workspace-two", "study", "alpha")
    assert (
        GitCoordinator(provider.open("workspace-two").root).head()
        != receipt["gitCommit"]
    )


@pytest.mark.parametrize(
    "program_id", ["../escape", "A", "a/b", "a\\b", "C:foo", "a.rs", "", "a\n"]
)
def test_unsafe_ids_rejected(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness], program_id: str
) -> None:
    with pytest.raises(ProgramArtifactError):
        programs[0].author("workspace-one", "study", program_id, STUDY_TEMPLATE)


def test_targeted_updates_and_read_ranges(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, _ = programs
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    assert alpha["interval"] is None
    assert alpha["symbols"] == ["SPY"]
    assert alpha["indicatorIds"] == []
    indicators = cast(
        list[dict[str, object]],
        service.list("workspace-one", "indicator")["programs"],
    )
    assert [entry["programId"] for entry in indicators] == ["last"]
    assert service.list("workspace-one", "study", prefix="be")["count"] == 1


def test_listing_bounds_empty_workspaces_and_unsafe_entries(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
            "interval": "1min",
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
    assert str(config["run_id"]).startswith("study-alpha-")
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
            "interval": "1min",
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


def test_sdk_templates_match_actual_source_signatures(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service = programs[0]
    consumer = (service.sdk_root / "src/consumer.rs").read_text(encoding="utf-8")
    program = (service.sdk_root / "src/program.rs").read_text(encoding="utf-8")
    indicator = (service.sdk_root / "src/indicator.rs").read_text(encoding="utf-8")
    assert "fn subscriptions(&self) -> Vec<Subscription>" in consumer
    assert (
        "fn on_bar(&mut self, _frame: &BarFrame, _context: &mut Self::Context)"
        in consumer
    )
    assert "StudyProgram: MarketConsumer<Context = ObservationContext>" in program
    assert "StrategyProgram: MarketConsumer<Context = OrderSink>" in program
    assert "fn publish(&mut self, name: &str, value: f64)" in program
    assert "fn value(&self) -> Option<Decimal>" in indicator
    for kind, template in PROGRAM_TEMPLATES.items():
        reference = service.read_sdk_reference(kind)
        assert reference["source"] == template
        assert reference["sdkVersion"] == "0.1.0" and reference["sdkAbiVersion"] == 4
    assert service.read_sdk_reference("consumer", start_line=19, end_line=22)[
        "source"
    ] == "".join(consumer.splitlines(keepends=True)[18:22])
    with pytest.raises(ProgramArtifactError):
        service.read_sdk_reference("../program")


def test_missing_harness_fails_closed(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    _, provider, _ = programs
    service = WorkspacePrograms(provider)
    service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    with pytest.raises(ProgramArtifactError, match="not configured"):
        service.validate("workspace-one", "study", "alpha")


def test_templates_compile_against_actual_sdk(
    tmp_path: Path, programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness]
) -> None:
    service = programs[0]
    crate = tmp_path / "templates"
    (crate / "src/bin").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "program-template-check"\nversion = "0.0.0"\nedition = "2021"\n'
        '[dependencies]\nharness-strategy-sdk = { path = "'
        + service.sdk_root.as_posix()
        + '" }\n',
        encoding="utf-8",
    )
    for kind, source in PROGRAM_TEMPLATES.items():
        configuration = {
            **CONFIG,
            "subscriptions": [
                {"data_type": "bars", "symbols": ["MSFT", "AAPL"], "interval": "1d"},
                {"data_type": "quote", "symbols": ["MSFT"]},
                {"data_type": "trade", "symbols": ["AAPL"]},
            ],
        }
        source = service.template_source(cast(ProgramKind, kind), configuration)
        if kind == "indicator":
            source = service._assembled_source(
                {
                    "source": source,
                    "dependencies": {},
                    "manifest": {"indicatorType": "AuthoredIndicator"},
                },
                "indicator",
            )
        (crate / "src/bin" / f"{kind}.rs").write_text(
            source + "\nfn main() {}\n", encoding="utf-8"
        )
    result = subprocess.run(
        [
            "cargo",
            "check",
            "--offline",
            "--bins",
            "--manifest-path",
            str(crate / "Cargo.toml"),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_configuration_cannot_override_committed_source(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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


def test_staged_artifact_changes_are_not_overwritten(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, _ = programs
    receipt = service.author(
        "workspace-one", "study", "alpha", STUDY_TEMPLATE, configuration=CONFIG
    )
    root = provider.open("workspace-one").root
    git = GitCoordinator(root)
    path = provider.resolve_path("workspace-one", "studies/alpha/program.rs")
    path.write_text("staged change", encoding="utf-8")
    git._run("add", "--", "studies/alpha/program.rs")
    path.write_bytes(STUDY_TEMPLATE.encode())
    with pytest.raises(ProgramArtifactError, match="staged"):
        service.update(
            "workspace-one",
            "study",
            "alpha",
            expected_version=cast(str, receipt["version"]),
            expected="self.frames += 1",
            replacement="self.frames += 2",
        )


def test_strategy_dispatch_and_indicator_dependency_validation(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    assert service.run("workspace-one", "strategy", "alpha")["ok"] is True
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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


def test_pinned_load_and_completion_verification(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs
    dep = service.author(
        "workspace-one", "indicator", "last", INDICATOR_TEMPLATE, configuration=CONFIG
    )
    first = service.author(
        "workspace-one",
        "study",
        "alpha",
        STUDY_TEMPLATE,
        configuration=CONFIG,
        indicator_ids=["last"],
    )
    pinned = service.load(
        "workspace-one", "study", "alpha", cast(str, first["gitCommit"])
    )
    assert (
        pinned["source"]
        == "mod r#last {\n" + INDICATOR_TEMPLATE + "\n}\n" + STUDY_TEMPLATE
    )
    assert pinned["configuration"] == CONFIG
    assert (
        cast(list[dict[str, object]], pinned["dependencies"])[0]["sourceSha256"]
        == dep["sourceSha256"]
    )
    service.update(
        "workspace-one",
        "indicator",
        "last",
        expected_version=cast(str, dep["version"]),
        expected="bar.close",
        replacement="bar.open",
    )
    assert (
        service.load("workspace-one", "study", "alpha")["version"] != first["version"]
    )
    assert service.verify_completion("workspace-one", "study", "alpha", first) == pinned
    assert (
        service.load("workspace-one", "study", "alpha", cast(str, first["gitCommit"]))
        == pinned
    )
    with pytest.raises(ProgramArtifactError, match="does not match"):
        service.verify_completion(
            "workspace-one", "study", "alpha", {**first, "version": "0" * 64}
        )
    with pytest.raises(ProgramArtifactError, match="reachable"):
        service.load("workspace-two", "study", "alpha", cast(str, first["gitCommit"]))
    with pytest.raises(ProgramArtifactError, match="object ID"):
        service.load("workspace-one", "study", "alpha", "HEAD")


def test_basic_template_defaults_follow_declared_inputs(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
    change: dict[str, object],
) -> None:
    service, provider, _ = programs
    before = GitCoordinator(provider.open("workspace-one").root).head()
    with pytest.raises(ProgramArtifactError):
        service.author(
            "workspace-one",
            "study",
            "alpha",
            STUDY_TEMPLATE,
            configuration={**CONFIG, **change},
        )
    assert GitCoordinator(provider.open("workspace-one").root).head() == before


def test_sdk_root_deployment_and_version_mismatch(
    tmp_path: Path,
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, harness = programs
    assert service.sdk_root.resolve() == Path("src/harness/strategy-sdk").resolve()
    missing = WorkspacePrograms(provider, harness, sdk_root=tmp_path / "not-deployed")
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
    matched = WorkspacePrograms(provider, harness, sdk_root=deployed)
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, _ = programs
    source = "// " + "x" * 20000 + "\n"
    service.author("workspace-one", "study", "long", source, configuration=CONFIG)
    result = service.read("workspace-one", "study", "long", max_chars=100)
    assert result["source"] == source[:100] and result["truncated"] is True
    reference = service.read_sdk_reference("program", max_chars=100)
    assert len(cast(str, reference["source"])) == 100 and reference["truncated"] is True
    assert service.read_sdk_reference("indicator_api")["source"] == (
        service.sdk_root / "src/indicator.rs"
    ).read_text(encoding="utf-8")
    decimal = service.read_sdk_reference("decimal", start_line=260, end_line=300)
    assert "pub fn to_f64" in cast(str, decimal["source"])
    assert "to_f64_checked" in cast(str, decimal["source"])


def test_configuration_diagnostics_are_actionable(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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


def test_service_uses_no_private_git_methods() -> None:
    source = Path("src/thesis/workspaces/programs.py").read_text(encoding="utf-8")
    assert "git._run" not in source


def test_sdk_wheel_assets_are_canonical_and_portable(tmp_path: Path) -> None:
    """Build from a fresh tree so stale local build/lib cannot mask missing data."""
    project = tmp_path / "project"
    project.mkdir()
    for filename in ("pyproject.toml", "MANIFEST.in"):
        shutil.copyfile(filename, project / filename)
    for package in ("thesis", "lake"):
        shutil.copytree(
            Path("src") / package,
            project / "src" / package,
            ignore=shutil.ignore_patterns("target", "__pycache__"),
        )
    for crate in ("strategy-sdk", "contracts", "num"):
        source = Path("src/harness") / crate
        destination = project / "src/harness" / crate
        shutil.copytree(source / "src", destination / "src")
        shutil.copyfile(source / "Cargo.toml", destination / "Cargo.toml")
    output = tmp_path / "dist"
    result = subprocess.run(
        [
            "uv",
            "build",
            "--python",
            "3.12",
            "--wheel",
            "--force-pep517",
            "--out-dir",
            str(output),
            "--clear",
            "--no-create-gitignore",
            str(project),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "installed"
    with zipfile.ZipFile(next(output.glob("*.whl"))) as wheel:
        names = wheel.namelist()
        assert not any("/target/" in name for name in names)
        for crate in ("strategy-sdk", "contracts", "num"):
            root = Path("src/harness") / crate
            for source in [root / "Cargo.toml", *sorted((root / "src").glob("*.rs"))]:
                assert (
                    wheel.read(source.relative_to("src").as_posix())
                    == source.read_bytes()
                )
        wheel.extractall(installed)
    sdk = installed / "harness/strategy-sdk"
    manifest = tomllib.loads((sdk / "Cargo.toml").read_text(encoding="utf-8"))
    for dependency in ("num", "harness-contracts"):
        path = manifest["dependencies"][dependency]["path"]
        assert not Path(path).is_absolute()
        assert (sdk / path / "Cargo.toml").is_file()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from thesis.workspaces.programs import WorkspacePrograms; "
            "service = WorkspacePrograms(None); "
            "assert service.sdk_root == Path(sys.argv[1]) / 'harness/strategy-sdk'; "
            "assert service.read_sdk_reference('consumer')['sdkIdentity']; "
            "assert service.read_sdk_reference('study')['sdkVersion'] == sys.argv[2]",
            str(installed),
            manifest["package"]["version"],
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_publish_leaves_missing_checkout_without_new_commit(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, _ = programs
    receipt = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    source = provider.resolve_path("workspace-one", "studies/alpha/program.rs")
    source.unlink()
    assert service.publish("workspace-one", "study", "alpha") == receipt
    assert not source.exists()
    git = GitCoordinator(provider.open("workspace-one").root)
    assert git.head() == receipt["gitCommit"]
    assert not git._run("diff", "--cached", "--name-only").stdout.strip()


def test_publish_preserves_staged_operator_edits(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, _ = programs
    receipt = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    source = provider.resolve_path("workspace-one", "studies/alpha/program.rs")
    source.write_text("operator edit", encoding="utf-8")
    git = GitCoordinator(provider.open("workspace-one").root)
    git._run("add", "--", "studies/alpha/program.rs")
    source.write_text(service.template_source("study", CONFIG), encoding="utf-8")
    assert service.publish("workspace-one", "study", "alpha") == receipt
    assert git.head() == receipt["gitCommit"]
    assert git._run("show", ":studies/alpha/program.rs").stdout == "operator edit"


def test_publish_preserves_deliberately_staged_parent_commit(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, _ = programs
    first = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    source = provider.resolve_path("workspace-one", "studies/alpha/program.rs")
    old = source.read_bytes()
    latest = service.update(
        "workspace-one",
        "study",
        "alpha",
        expected_version=cast(str, first["version"]),
        expected="self.frames += 1",
        replacement="self.frames += 2",
    )
    source.write_bytes(old)
    git = GitCoordinator(provider.open("workspace-one").root)
    git._run("add", "--", "studies/alpha/program.rs")
    index = (git.repository / ".git/index").read_bytes()
    assert service.publish("workspace-one", "study", "alpha") == latest
    assert source.read_bytes() == old
    assert (git.repository / ".git/index").read_bytes() == index


def test_publish_does_not_repair_interrupted_update_materialization(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, provider, _ = programs
    first = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    replace = os.replace
    root = provider.open("workspace-one").root

    def fail(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == root / "studies/alpha/program.rs":
            raise OSError("injected checkout failure")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ProgramPublicationError) as error:
        service.update(
            "workspace-one",
            "study",
            "alpha",
            expected_version=cast(str, first["version"]),
            expected="self.frames += 1",
            replacement="self.frames += 2",
        )
    receipt = error.value.receipt
    assert receipt["gitCommit"] != first["gitCommit"]
    assert "self.frames += 2" in str(
        service.read("workspace-one", "study", "alpha")["source"]
    )
    monkeypatch.setattr(os, "replace", replace)
    assert service.publish("workspace-one", "study", "alpha") == receipt
    assert "self.frames += 1" in (root / "studies/alpha/program.rs").read_text(
        encoding="utf-8"
    )
    git = GitCoordinator(root)
    assert (
        git._run("diff", "--cached", "--name-only").stdout.strip()
        == "studies/alpha/program.rs"
    )
    assert not git._run("diff", "--name-only").stdout.strip()


def test_sdk_dependency_change_invalidates_validation(
    tmp_path: Path,
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, harness = programs
    for crate in ("strategy-sdk", "contracts", "num"):
        shutil.copytree(service.sdk_root.parent / crate, tmp_path / crate)
    deployed = WorkspacePrograms(provider, harness, sdk_root=tmp_path / "strategy-sdk")
    deployed.author("workspace-one", "study", "alpha", configuration=CONFIG)
    deployed.validate("workspace-one", "study", "alpha")
    dependency = tmp_path / "contracts/src/lib.rs"
    dependency.write_text(
        dependency.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8"
    )
    with pytest.raises(ProgramArtifactError, match="SDK identities"):
        deployed.run("workspace-one", "study", "alpha")


def test_latest_committed_source_configuration_and_both_symbol_scopes(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, harness = programs
    first = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    config = {
        **CONFIG,
        "subscriptions": [
            {"data_type": "bars", "symbols": ["MSFT", "AAPL"], "interval": "1d"},
            {"data_type": "quote", "symbols": ["MSFT"]},
        ],
        "data": {"requiredSymbols": ["MSFT", "AAPL"], "interval": "1d"},
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
    assert document["symbols"] == instance["symbols"] == ["AAPL", "MSFT"]
    assert cast(dict[str, object], instance["parameters"])["source"] == updated_source
    assert document["data"] == {
        "symbols": ["AAPL", "MSFT"],
        "subscriptions": config["subscriptions"],
    }


def test_load_stored_validation_at_selected_commit(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
    compiler: str | None,
) -> None:
    service, provider, harness = programs
    authored = service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    validated = service.validate("workspace-one", "study", "alpha")
    pinned = service.load("workspace-one", "study", "alpha")
    assert pinned["validationIdentity"] == "compiler-one"
    head = GitCoordinator(provider.open("workspace-one").root).head()
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
    assert GitCoordinator(provider.open("workspace-one").root).head() == head


def test_run_configuration_matches_native_authored_document_semantics(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    service.run("workspace-one", "study", "alpha")
    document = cast(dict[str, object], harness.calls[-1][1]["config"])
    assert document["start"] == CONFIG["start"] and document["end"] == CONFIG["end"]
    assert not {
        "subscriptions",
        "interval",
        "requiredSymbols",
        "required_symbols",
    }.intersection(document)
    assert document["symbols"] == ["SPY"]
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
    assert tick_document["symbols"] == ["MSFT"]


def test_existing_provider_and_raw_client_injection(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
                    "structuredContent": {"valid": True, "run_id": "run-one"},
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


def test_run_ids_are_workspace_scoped_and_unique_per_explicit_invocation(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    ids = []
    for workspace in ("workspace-one", "workspace-two"):
        service.author(
            workspace,
            "study",
            "alpha",
            configuration={**CONFIG, "run_id": "caller-fixed"},
        )
        service.validate(workspace, "study", "alpha")
        for _ in range(2):
            receipt = service.run(workspace, "study", "alpha")
            submitted = cast(dict[str, object], harness.calls[-1][1]["config"])[
                "run_id"
            ]
            assert receipt["submittedRunId"] == submitted
            assert cast(dict[str, object], receipt["result"])["run_id"] == submitted
            ids.append(cast(str, submitted))
    assert len(set(ids)) == 4
    assert ids[0].rsplit("-", 1)[0] == ids[1].rsplit("-", 1)[0]
    assert ids[0].rsplit("-", 1)[0] != ids[2].rsplit("-", 1)[0]


@pytest.mark.parametrize("identity", [None, "compiler-two"])
def test_remote_compiler_identity_changes_fail_closed_before_run(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, _, harness = programs
    harness.validation_identity = None
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    receipt = service.validate("workspace-one", "study", "alpha")
    assert receipt["accepted"] is False
    assert "identity is unavailable" in cast(str, receipt["message"])


def test_raw_transport_unknown_run_outcome_retains_submitted_id(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    submitted = cast(dict[str, object], harness.calls[-1][1]["config"])["run_id"]
    assert receipt["ok"] is False and receipt["executionCertainty"] == "unknown_outcome"
    assert receipt["submittedRunId"] == submitted
    assert cast(dict[str, object], receipt["result"])["run_id"] == submitted
    assert "never automatically replay" in cast(str, receipt["retryGuidance"])


def test_fresh_public_facade_identity_blocks_remote_changes(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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


def test_compact_diagnostics_keep_repair_spans_and_children_without_excerpts() -> None:
    outcome = WorkspacePrograms._outcome(
        McpToolResult(
            tool="validate_study",
            is_error=True,
            structured={
                "diagnostics": [
                    {
                        "message": "missing method",
                        "level": "error",
                        "code": {"code": "E0599", "explanation": "large explanation"},
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
                                        "suggestion_applicability": "MachineApplicable",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        ),
        validation=True,
    )
    detail = cast(list[dict[str, object]], outcome["diagnostics"])[0]
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
    assert STUDY_TEMPLATE not in str(outcome)
    assert "explanation" not in str(outcome) and "rendered" not in str(outcome)


def test_serving_sdk_mismatch_blocks_templates_validation_run_and_completion(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
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
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    service, provider, harness = programs
    service.author("workspace-one", "study", "alpha", configuration=CONFIG)
    before = GitCoordinator(provider.open("workspace-one").root).head()
    harness.during = lambda: setattr(harness, "validation_identity", "compiler-two")
    with pytest.raises(
        ProgramArtifactError, match="compiler changed during validation"
    ):
        service.validate("workspace-one", "study", "alpha")
    assert GitCoordinator(provider.open("workspace-one").root).head() == before


def test_local_sdk_guidance_and_artifact_reads_work_without_harness(
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    _, provider, _ = programs
    local = WorkspacePrograms(provider)
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


def test_native_study_validation_and_run_document_compile(
    tmp_path: Path,
    programs: tuple[WorkspacePrograms, LocalWorkspaceProvider, Harness],
) -> None:
    """Optional built-binary smoke: real JIT compile, load, and subscriptions.

    No replay/data fetch is attempted. Validate the run document's exact assembled
    source and data echo through the native parser, JIT compiler, and ABI loader.
    """
    binary = Path(
        "src/harness/target/debug/mcp.exe"
        if os.name == "nt"
        else "src/harness/target/debug/mcp"
    )
    if not binary.is_file():
        pytest.skip(
            "native MCP binary not built; template compilation is covered separately"
        )
    service, _, harness = programs
    config = {
        **CONFIG,
        "subscriptions": [{"data_type": "bars", "symbols": ["MSFT"], "interval": "1d"}],
    }
    service.author("workspace-one", "indicator", "last", configuration=config)
    source = service.template_source("study", config).replace(
        "struct AuthoredStudy { frames: u64 }",
        "struct AuthoredStudy { frames: u64, last: crate::last::AuthoredIndicator }",
    )
    source = source.replace(
        "type Context = ObservationContext;",
        "type Context = ObservationContext;\n"
        "    fn indicators(&mut self) -> Vec<&mut dyn harness_strategy_sdk::Indicator> { vec![&mut self.last] }",
    )
    service.author(
        "workspace-one",
        "study",
        "native",
        source,
        configuration=config,
        indicator_ids=["last"],
    )
    service.validate("workspace-one", "study", "native")
    service.run("workspace-one", "study", "native")
    run_document = cast(dict[str, object], harness.calls[-1][1]["config"])
    instance = cast(list[dict[str, object]], run_document["strategy_instances"])[0]
    source = cast(dict[str, object], instance["parameters"])["source"]
    data = cast(dict[str, object], run_document["data"])
    model = {
        "source": source,
        "harnessConfiguration": {
            "start": run_document["start"],
            "end": run_document["end"],
            "subscriptions": data["subscriptions"],
            "symbols": run_document["symbols"],
        },
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "read_sdk_reference",
                "arguments": {"topic": "consumer", "start_line": 19, "end_line": 22},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "validate_study", "arguments": {"model": model}},
        },
    ]
    environment = {
        **os.environ,
        "HARNESS_JIT_DIR": str(tmp_path / "jit"),
        "HARNESS_SDK_DIR": str(service.sdk_root.resolve()),
    }
    result = subprocess.run(
        [str(binary.resolve())],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    responses = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    response = next(item for item in responses if item.get("id") == 2)
    server = next(item for item in responses if item.get("id") == 1)["result"][
        "serverInfo"
    ]
    reference = next(item for item in responses if item.get("id") == 3)["result"][
        "structuredContent"
    ]
    assert server["sdkIdentity"] == reference["sdkIdentity"] == service._sdk_identity()
    assert (server["sdkVersion"], server["sdkAbiVersion"]) == service._sdk_metadata(
        service.sdk_root
    )
    assert reference["content"] == "\n".join(
        (service.sdk_root / "src/consumer.rs")
        .read_text(encoding="utf-8")
        .splitlines()[18:22]
    )
    assert "error" not in response, response
    payload = response["result"]
    assert not payload.get("isError"), payload
    assert payload["structuredContent"]["valid"] is True
    assert payload["structuredContent"]["subscriptions"] == data["subscriptions"]
