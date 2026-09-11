"""Focused tests for the local stack verification script.

Fixed fakes only: no network, no clock, no randomness.  The verification
driver is loaded by file path because scripts/local is a script directory,
not an installed package, and every HTTP boundary is injected, so no test
touches a real socket.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "local" / "run_local_stack.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_local_stack", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_local_stack = _load_module()

THESIS_TOKEN = "thesis-token-value"
HARNESS_TOKEN = "harness-token-value"
WORKSPACE = "local-stack"

RouteResponse = tuple[int, Mapping[str, object] | None]
Route = RouteResponse | Callable[[str | None], RouteResponse]


class FakeTransport:
    """Routing fake over (method, path); unhandled routes raise TransportError."""

    def __init__(self, routes: Mapping[tuple[str, str], Route]) -> None:
        self._routes = dict(routes)
        self.calls: list[tuple[str, str, str | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> run_local_stack.HttpResponse:
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        self.calls.append((method, path, token))
        route = self._routes.get((method, path))
        if route is None:
            raise run_local_stack.TransportError(f"no fake route for {method} {path}")
        status, body = route if isinstance(route, tuple) else route(token)
        return run_local_stack.HttpResponse(status_code=status, body=body)


class BrokenTransport:
    """Transport that always fails, simulating an unreachable service."""

    def __init__(self, message: str = "connection refused") -> None:
        self._message = message

    def request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> run_local_stack.HttpResponse:
        raise run_local_stack.TransportError(self._message)


def _auth_check(token: str | None) -> RouteResponse:
    if token:
        return 200, {"status": "ok"}
    return 401, {"error": {"code": "unauthorized", "message": "missing credentials"}}


def thesis_routes() -> dict[tuple[str, str], Route]:
    workspace = f"/v1/workspaces/{WORKSPACE}"
    return {
        ("GET", "/health"): (200, {"status": "ok"}),
        ("GET", "/v1/auth/check"): _auth_check,
        ("GET", "/v1/workspaces"): (200, {"items": [{"id": WORKSPACE}]}),
        ("POST", f"{workspace}/chats"): (200, {"id": "chat-001"}),
        ("POST", f"{workspace}/chats/chat-001/messages"): (200, {"id": "msg-001"}),
        ("POST", f"{workspace}/backtests/launch"): (
            200,
            {"job_id": "job-001", "state": "queued"},
        ),
    }


def make_context(
    transport, *, thesis_token: str | None = THESIS_TOKEN
) -> run_local_stack.StackContext:
    return run_local_stack.StackContext(
        transport=transport,
        thesis_url=run_local_stack.DEFAULT_THESIS_URL,
        harness_url=run_local_stack.DEFAULT_HARNESS_URL,
        thesis_token=thesis_token,
        harness_token=HARNESS_TOKEN,
        workspace=WORKSPACE,
        bundle_ref="bundle-test",
        config_path="config/harness/backtest.yaml",
    )


# ---------------------------------------------------------------------------
# Stage result aggregation
# ---------------------------------------------------------------------------


def test_exit_code_is_zero_only_when_every_stage_passed() -> None:
    passing = run_local_stack.StageResult("a", True, "ok")
    failing = run_local_stack.StageResult("b", False, "bad", "fix it")

    assert run_local_stack.exit_code((passing,)) == 0
    assert run_local_stack.exit_code((passing, failing)) == 1
    assert run_local_stack.exit_code(()) == 1


def test_run_stages_skips_disabled_stages() -> None:
    transport = FakeTransport(thesis_routes())
    results = run_local_stack.run_stages(
        make_context(transport), skip_chat=True, skip_backtest=True
    )

    assert [result.stage for result in results] == [
        run_local_stack.STAGE_HARNESS,
        run_local_stack.STAGE_THESIS,
        run_local_stack.STAGE_WORKSPACE,
    ]
    assert run_local_stack.exit_code(results) == 0


def test_full_enabled_run_passes_and_reports_every_stage() -> None:
    transport = FakeTransport(thesis_routes())
    results = run_local_stack.run_stages(make_context(transport))

    assert [result.stage for result in results] == list(run_local_stack.STAGE_IDS)
    assert all(result.passed for result in results)
    assert run_local_stack.exit_code(results) == 0


# ---------------------------------------------------------------------------
# Remediation message mapping
# ---------------------------------------------------------------------------


def test_every_registered_stage_has_a_remediation_hint() -> None:
    for stage in run_local_stack.STAGE_IDS:
        hint = run_local_stack.remediation_for(stage)
        assert stage in run_local_stack.REMEDIATIONS
        assert len(hint.strip()) > 0


def test_thesis_remediation_names_the_server_token_variable() -> None:
    hint = run_local_stack.remediation_for(run_local_stack.STAGE_THESIS)
    assert "THESIS_API__BEARER_TOKEN" in hint


def test_backtest_remediation_names_the_harness_boundary_composition() -> None:
    transport = FakeTransport(
        {("POST", "/v1/workspaces/local-stack/backtests/launch"): (503, None)}
    )
    result = run_local_stack.check_backtest(make_context(transport))

    assert not result.passed
    assert result.remediation == run_local_stack.remediation_for(
        run_local_stack.STAGE_BACKTEST
    )
    assert "build_local_workspace_data(harness_runner=...)" in result.remediation


def test_unreachable_stages_fail_with_mapped_remediation() -> None:
    ctx = make_context(BrokenTransport("connection refused"))
    results = run_local_stack.run_stages(ctx)

    assert results
    assert all(not result.passed for result in results)
    for result in results:
        base_hint = run_local_stack.remediation_for(result.stage)
        assert result.remediation
        assert result.remediation.startswith(base_hint[:20])


# ---------------------------------------------------------------------------
# Token redaction in rendered output
# ---------------------------------------------------------------------------


def test_redactor_masks_every_known_secret_value() -> None:
    redactor = run_local_stack.SecretRedactor((THESIS_TOKEN, HARNESS_TOKEN, None))
    rendered = redactor.apply(
        f"bearer {THESIS_TOKEN} for harness {HARNESS_TOKEN} leaked"
    )

    assert THESIS_TOKEN not in rendered
    assert HARNESS_TOKEN not in rendered
    assert rendered.count("***redacted***") == 2


def test_render_report_never_prints_token_values() -> None:
    results = (
        run_local_stack.StageResult(
            "s1", False, f"token {THESIS_TOKEN} was rejected", f"set {THESIS_TOKEN}"
        ),
        run_local_stack.StageResult("s2", True, f"ok with {THESIS_TOKEN}"),
    )
    report = run_local_stack.render_report(
        results, run_local_stack.SecretRedactor((THESIS_TOKEN,))
    )

    assert THESIS_TOKEN not in report
    assert "***redacted***" in report
    assert "[FAIL] s1" in report
    assert "[PASS] s2" in report
    assert report.endswith("stages: 1/2 passed")


def test_render_report_omits_fix_line_for_passing_stages() -> None:
    result = run_local_stack.StageResult("s1", True, "ok", "unused")
    report = run_local_stack.render_report(
        (result,), run_local_stack.SecretRedactor(())
    )

    assert "fix:" not in report
    assert report.endswith("stages: 1/1 passed")


# ---------------------------------------------------------------------------
# CLI argument parsing defaults and token resolution
# ---------------------------------------------------------------------------


def test_cli_defaults_match_the_local_stack_convention() -> None:
    args = run_local_stack.build_parser().parse_args([])

    assert args.thesis_url == run_local_stack.DEFAULT_THESIS_URL
    assert args.harness_url == run_local_stack.DEFAULT_HARNESS_URL
    assert args.token is None
    assert args.workspace == run_local_stack.DEFAULT_WORKSPACE
    assert args.bundle_ref == run_local_stack.DEFAULT_BUNDLE_REF
    assert args.config_path == run_local_stack.DEFAULT_CONFIG_PATH
    assert args.skip_chat is False
    assert args.skip_backtest is False


def test_resolve_tokens_reads_the_documented_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("THESIS_API_BEARER_TOKEN", THESIS_TOKEN)
    monkeypatch.setenv("HARNESS_API_BEARER_TOKEN", HARNESS_TOKEN)

    thesis, harness = run_local_stack.resolve_tokens(None)

    assert (thesis, harness) == (THESIS_TOKEN, HARNESS_TOKEN)


def test_resolve_tokens_falls_back_to_the_server_spelling(monkeypatch) -> None:
    monkeypatch.delenv("THESIS_API_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("THESIS_API__BEARER_TOKEN", THESIS_TOKEN)
    monkeypatch.delenv("HARNESS_API_BEARER_TOKEN", raising=False)

    thesis, harness = run_local_stack.resolve_tokens(None)

    assert thesis == THESIS_TOKEN
    assert harness is None


def test_token_override_applies_to_both_apis(monkeypatch) -> None:
    monkeypatch.setenv("THESIS_API_BEARER_TOKEN", THESIS_TOKEN)
    monkeypatch.setenv("HARNESS_API_BEARER_TOKEN", HARNESS_TOKEN)

    thesis, harness = run_local_stack.resolve_tokens("override")

    assert (thesis, harness) == ("override", "override")


def test_resolve_tokens_without_configuration_is_none(monkeypatch) -> None:
    monkeypatch.delenv("THESIS_API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("THESIS_API__BEARER_TOKEN", raising=False)
    monkeypatch.delenv("HARNESS_API_BEARER_TOKEN", raising=False)

    assert run_local_stack.resolve_tokens(None) == (None, None)


# ---------------------------------------------------------------------------
# Injected-boundary stage behavior
# ---------------------------------------------------------------------------


def test_harness_stage_requires_health_ok_and_accepts_bearer() -> None:
    transport = FakeTransport(
        {
            ("GET", "/health"): (200, {"status": "ok"}),
            ("GET", "/v1/auth/check"): (200, {"status": "ok"}),
        }
    )
    passing = run_local_stack.check_harness(make_context(transport))
    assert passing.passed

    sick = run_local_stack.check_harness(
        make_context(FakeTransport({("GET", "/health"): (503, None)}))
    )
    assert not sick.passed
    assert "HTTP 503" in sick.detail
    assert sick.remediation == run_local_stack.remediation_for(
        run_local_stack.STAGE_HARNESS
    )


def test_harness_stage_passes_unverified_without_a_harness_token() -> None:
    transport = FakeTransport({("GET", "/health"): (200, {"status": "ok"})})
    ctx = run_local_stack.StackContext(
        transport=transport,
        thesis_url=run_local_stack.DEFAULT_THESIS_URL,
        harness_url=run_local_stack.DEFAULT_HARNESS_URL,
        thesis_token=None,
        harness_token=None,
        workspace=WORKSPACE,
        bundle_ref="bundle-test",
        config_path="config/harness/backtest.yaml",
    )

    result = run_local_stack.check_harness(ctx)

    assert result.passed
    assert "bearer not verified" in result.detail


def test_thesis_auth_boundary_requires_anonymous_401() -> None:
    open_boundary = FakeTransport(
        {
            ("GET", "/health"): (200, {"status": "ok"}),
            ("GET", "/v1/auth/check"): (200, {"status": "ok"}),
        }
    )
    result = run_local_stack.check_thesis(make_context(open_boundary))

    assert not result.passed
    assert "auth boundary is open" in result.detail
    assert "THESIS_API__BEARER_TOKEN" in result.remediation


def test_thesis_stage_accepts_fail_closed_boundary_with_bearer() -> None:
    result = run_local_stack.check_thesis(make_context(FakeTransport(thesis_routes())))

    assert result.passed
    assert "anonymous 401" in result.detail


def test_workspace_stage_reports_the_create_call_when_missing() -> None:
    transport = FakeTransport({("GET", "/v1/workspaces"): (200, {"items": []})})
    result = run_local_stack.check_workspace(make_context(transport))

    assert not result.passed
    assert "POST" in result.remediation
    assert "/v1/workspaces" in result.remediation
    assert THESIS_TOKEN not in result.remediation


def test_chat_stage_posts_through_the_chat_routes() -> None:
    transport = FakeTransport(thesis_routes())
    result = run_local_stack.check_chat(make_context(transport))

    assert result.passed
    assert "chat-001" in result.detail
    assert "msg-001" in result.detail
    methods = [(method, path) for method, path, _ in transport.calls]
    assert ("POST", "/v1/workspaces/local-stack/chats") in methods
    assert ("POST", "/v1/workspaces/local-stack/chats/chat-001/messages") in methods


def test_backtest_stage_accepts_job_id_or_local_exit_code() -> None:
    launch_path = "/v1/workspaces/local-stack/backtests/launch"

    remote = run_local_stack.check_backtest(
        make_context(
            FakeTransport(
                {("POST", launch_path): (200, {"job_id": "job-001", "state": "queued"})}
            )
        )
    )
    assert remote.passed
    assert "job_id=job-001" in remote.detail

    local = run_local_stack.check_backtest(
        make_context(
            FakeTransport(
                {
                    ("POST", launch_path): (
                        200,
                        {"exit_code": 0, "job_id": None, "state": None},
                    )
                }
            )
        )
    )
    assert local.passed
    assert "exit_code=0" in local.detail

    empty = run_local_stack.check_backtest(
        make_context(FakeTransport({("POST", launch_path): (200, {})}))
    )
    assert not empty.passed
