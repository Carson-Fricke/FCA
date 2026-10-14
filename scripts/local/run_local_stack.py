"""Local end-to-end stack verification for the Thesis loopback stack.

Exercises the TH6.10 chain in-process over injected boundaries: the harness
quant API, the Thesis API auth boundary, the workspace catalog, the chat
message route, and harness-delegated backtest launch.  This is a verifier,
not a process manager: it expects the stack to be running and reports
PASS/FAIL per stage with remediation hints.  The HTTP boundary is injected so
tests exercise the pure logic with fakes instead of real sockets.  Token
values are never printed: every rendered line passes through redaction.

Usage (from the repository root, against a running stack):

    uv run python scripts/local/run_local_stack.py

Exit code 0 means every enabled stage passed; any failed or skipped-for-failure
stage exits 1 with per-stage remediation hints.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

DEFAULT_THESIS_URL = "http://127.0.0.1:8000"
DEFAULT_HARNESS_URL = "http://127.0.0.1:8001"
DEFAULT_WORKSPACE = "local-stack"
DEFAULT_BUNDLE_REF = "local-stack-verification"
DEFAULT_CONFIG_PATH = "config/harness/backtest.yaml"
DEFAULT_TIMEOUT_SECONDS = 10.0

THESIS_TOKEN_ENV = "THESIS_API_BEARER_TOKEN"
THESIS_TOKEN_ENV_SERVER = "THESIS_API__BEARER_TOKEN"
HARNESS_TOKEN_ENV = "HARNESS_API_BEARER_TOKEN"

STAGE_HARNESS = "harness-api-health"
STAGE_THESIS = "thesis-api-auth-boundary"
STAGE_WORKSPACE = "workspace-catalog"
STAGE_CHAT = "chat-message"
STAGE_BACKTEST = "backtest-launch"

STAGE_IDS: tuple[str, ...] = (
    STAGE_HARNESS,
    STAGE_THESIS,
    STAGE_WORKSPACE,
    STAGE_CHAT,
    STAGE_BACKTEST,
)

REMEDIATIONS: Mapping[str, str] = {
    STAGE_HARNESS: (
        "start the harness quant API (terminal 1): set HARNESS_API_BEARER_TOKEN, "
        "then run `uv run harness api --config config/harness/api.yaml` "
        "(binds http://127.0.0.1:8001); see "
        ".github/runbooks/thesis-local-stack.md section 3"
    ),
    STAGE_THESIS: (
        "start the Thesis API (terminal 2) with the serve composition piped to "
        "`uv run python -` and THESIS_API__BEARER_TOKEN set (runbook section 3); "
        "a 401 on /v1/auth/check with a token supplied means the presented token "
        f"does not equal {THESIS_TOKEN_ENV_SERVER}"
    ),
    STAGE_WORKSPACE: (
        "create the workspace with POST <thesis-url>/v1/workspaces (body "
        '`{"name": "<workspace-name>"}`) using the bearer from '
        f"{THESIS_TOKEN_ENV_SERVER}, then re-run with --workspace <returned id>; "
        "PowerShell example in runbook section 4"
    ),
    STAGE_CHAT: (
        "the chat route requires a valid bearer; confirm the thesis-api-auth-"
        f"boundary stage passes and that the token matches {THESIS_TOKEN_ENV_SERVER}"
    ),
    STAGE_BACKTEST: (
        "HTTP 503 harness_unavailable means the Thesis process was started "
        "without a harness boundary: restart the serve composition with "
        "build_local_workspace_data(harness_runner=...) (runbook section 3, "
        "terminal 2) and confirm the config path exists "
        f"({DEFAULT_CONFIG_PATH} by default)"
    ),
}


class TransportError(RuntimeError):
    """Raised when the injected transport cannot complete a request."""


@dataclass(frozen=True)
class HttpResponse:
    """One decoded HTTP response observed through the transport boundary."""

    status_code: int
    body: Mapping[str, object] | None


class Transport(Protocol):
    """HTTP request surface implemented by httpx here or a fake in tests."""

    def request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> HttpResponse: ...


class HttpxTransport:
    """Default transport over the repository httpx dependency."""

    def __init__(self, timeout_seconds: float) -> None:
        self._client = httpx.Client(timeout=timeout_seconds)

    def request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> HttpResponse:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._client.request(method, url, headers=headers, json=payload)
        except httpx.HTTPError as error:
            raise TransportError(f"{error.__class__.__name__}: {error}") from error
        try:
            decoded = response.json()
        except ValueError:
            decoded = None
        body = decoded if isinstance(decoded, dict) else None
        return HttpResponse(status_code=response.status_code, body=body)

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True, slots=True)
class StageResult:
    """Outcome of one verification stage."""

    stage: str
    passed: bool
    detail: str
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class StackContext:
    """Injected boundaries and operator selections for one verification run."""

    transport: Transport
    thesis_url: str
    harness_url: str
    thesis_token: str | None
    harness_token: str | None
    workspace: str
    bundle_ref: str
    config_path: str


class SecretRedactor:
    """Masks every known secret value before anything is rendered."""

    def __init__(self, secrets: Iterable[str | None]) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def apply(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***redacted***")
        return text


def _get(ctx: StackContext, url: str, *, token: str | None = None) -> HttpResponse:
    return ctx.transport.request("GET", url, token=token)


def _post(
    ctx: StackContext,
    url: str,
    payload: Mapping[str, object],
    *,
    token: str | None = None,
) -> HttpResponse:
    return ctx.transport.request("POST", url, token=token, payload=payload)


def check_harness(ctx: StackContext) -> StageResult:
    """Stage a: the harness quant API answers its unauthenticated health route."""
    url = f"{ctx.harness_url}/health"
    try:
        response = _get(ctx, url)
    except TransportError as error:
        return StageResult(
            STAGE_HARNESS,
            False,
            f"harness quant API unreachable at {url} ({error})",
            REMEDIATIONS[STAGE_HARNESS],
        )
    if response.status_code != 200:
        return StageResult(
            STAGE_HARNESS,
            False,
            f"harness quant API health returned HTTP {response.status_code}",
            REMEDIATIONS[STAGE_HARNESS],
        )
    status = (response.body or {}).get("status")
    if status != "ok":
        return StageResult(
            STAGE_HARNESS,
            False,
            f"harness quant API health body has status={status!r}, expected 'ok'",
            REMEDIATIONS[STAGE_HARNESS],
        )
    if ctx.harness_token is None:
        return StageResult(
            STAGE_HARNESS,
            True,
            "harness quant API healthy (bearer not verified: no token supplied)",
        )
    try:
        auth = _get(ctx, f"{ctx.harness_url}/v1/auth/check", token=ctx.harness_token)
    except TransportError as error:
        return StageResult(
            STAGE_HARNESS,
            False,
            f"harness quant API auth check unreachable ({error})",
            REMEDIATIONS[STAGE_HARNESS],
        )
    if auth.status_code != 200:
        return StageResult(
            STAGE_HARNESS,
            False,
            f"harness quant API rejected the bearer (HTTP {auth.status_code}); "
            f"the token must equal {HARNESS_TOKEN_ENV}",
            REMEDIATIONS[STAGE_HARNESS],
        )
    return StageResult(
        STAGE_HARNESS, True, "harness quant API healthy and authenticated"
    )


def check_thesis(ctx: StackContext) -> StageResult:
    """Stage b: Thesis API reachable and the anonymous auth boundary fails closed."""
    base = ctx.thesis_url
    try:
        health = _get(ctx, f"{base}/health")
        anonymous = _get(ctx, f"{base}/v1/auth/check")
    except TransportError as error:
        return StageResult(
            STAGE_THESIS,
            False,
            f"Thesis API unreachable at {base} ({error})",
            REMEDIATIONS[STAGE_THESIS],
        )
    if health.status_code != 200 or (health.body or {}).get("status") != "ok":
        return StageResult(
            STAGE_THESIS,
            False,
            f"Thesis API health returned HTTP {health.status_code} "
            f"(body status={(health.body or {}).get('status')!r})",
            REMEDIATIONS[STAGE_THESIS],
        )
    if anonymous.status_code != 401:
        return StageResult(
            STAGE_THESIS,
            False,
            f"Thesis API /v1/auth/check answered HTTP {anonymous.status_code} "
            "anonymously; the auth boundary is open (no bearer configured)",
            REMEDIATIONS[STAGE_THESIS],
        )
    if ctx.thesis_token is None:
        return StageResult(
            STAGE_THESIS,
            True,
            "Thesis API healthy; anonymous requests fail closed with 401 "
            "(bearer not verified: no token supplied)",
        )
    try:
        authed = _get(ctx, f"{base}/v1/auth/check", token=ctx.thesis_token)
    except TransportError as error:
        return StageResult(
            STAGE_THESIS,
            False,
            f"Thesis API auth check unreachable ({error})",
            REMEDIATIONS[STAGE_THESIS],
        )
    if authed.status_code != 200:
        return StageResult(
            STAGE_THESIS,
            False,
            f"Thesis API rejected the bearer (HTTP {authed.status_code}); "
            f"the token must equal {THESIS_TOKEN_ENV_SERVER}",
            REMEDIATIONS[STAGE_THESIS],
        )
    return StageResult(
        STAGE_THESIS, True, "Thesis API healthy, anonymous 401, bearer accepted"
    )


def check_workspace(ctx: StackContext) -> StageResult:
    """Stage c: the target workspace exists in the Thesis workspace catalog."""
    url = f"{ctx.thesis_url}/v1/workspaces"
    try:
        response = _get(ctx, url, token=ctx.thesis_token)
    except TransportError as error:
        return StageResult(
            STAGE_WORKSPACE,
            False,
            f"workspace catalog unreachable ({error})",
            REMEDIATIONS[STAGE_WORKSPACE],
        )
    if response.status_code == 401:
        return StageResult(
            STAGE_WORKSPACE,
            False,
            "workspace catalog returned 401; supply the Thesis bearer via "
            f"--token, {THESIS_TOKEN_ENV}, or {THESIS_TOKEN_ENV_SERVER}",
            REMEDIATIONS[STAGE_WORKSPACE],
        )
    if response.status_code != 200:
        return StageResult(
            STAGE_WORKSPACE,
            False,
            f"workspace catalog returned HTTP {response.status_code}",
            REMEDIATIONS[STAGE_WORKSPACE],
        )
    items = (response.body or {}).get("items")
    items = items if isinstance(items, list) else []
    known = [
        str(item.get("id"))
        for item in items
        if isinstance(item, Mapping) and item.get("id") is not None
    ]
    if ctx.workspace in known:
        return StageResult(
            STAGE_WORKSPACE, True, f"workspace {ctx.workspace!r} found in the catalog"
        )
    create_call = (
        f'POST {ctx.thesis_url}/v1/workspaces {{"name": "<workspace-name>"}} '
        "with the bearer, then re-run with "
        f"--workspace <returned-id> (catalog contains: {known or 'nothing'})"
    )
    return StageResult(
        STAGE_WORKSPACE,
        False,
        f"workspace {ctx.workspace!r} is not in the catalog",
        f"{REMEDIATIONS[STAGE_WORKSPACE]} — create call: {create_call}",
    )


def check_chat(ctx: StackContext) -> StageResult:
    """Stage d: a chat message posts through the Thesis API with the bearer."""
    base = f"{ctx.thesis_url}/v1/workspaces/{ctx.workspace}"
    try:
        chat = _post(
            ctx,
            f"{base}/chats",
            {"title": "Local stack verification", "owner_actor_id": "local-stack"},
            token=ctx.thesis_token,
        )
        if chat.status_code != 200:
            return StageResult(
                STAGE_CHAT,
                False,
                f"chat creation returned HTTP {chat.status_code}",
                REMEDIATIONS[STAGE_CHAT],
            )
        chat_id = (chat.body or {}).get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return StageResult(
                STAGE_CHAT,
                False,
                "chat creation returned no chat id",
                REMEDIATIONS[STAGE_CHAT],
            )
        message = _post(
            ctx,
            f"{base}/chats/{chat_id}/messages",
            {"actor_id": "local-stack", "body": "local stack verification message"},
            token=ctx.thesis_token,
        )
    except TransportError as error:
        return StageResult(
            STAGE_CHAT,
            False,
            f"chat surface unreachable ({error})",
            REMEDIATIONS[STAGE_CHAT],
        )
    if message.status_code != 200:
        return StageResult(
            STAGE_CHAT,
            False,
            f"chat message post returned HTTP {message.status_code}",
            REMEDIATIONS[STAGE_CHAT],
        )
    message_id = (message.body or {}).get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return StageResult(
            STAGE_CHAT,
            False,
            "chat message post returned no persisted message id",
            REMEDIATIONS[STAGE_CHAT],
        )
    return StageResult(
        STAGE_CHAT,
        True,
        f"chat {chat_id} accepted message {message_id}",
    )


def check_backtest(ctx: StackContext) -> StageResult:
    """Stage e: backtest launch delegates through the workspace data route."""
    url = f"{ctx.thesis_url}/v1/workspaces/{ctx.workspace}/backtests/launch"
    payload: dict[str, object] = {
        "bundle_ref": ctx.bundle_ref,
        "config": json.loads(Path(ctx.config_path).read_text(encoding="utf-8")),
        "environment": "paper",
    }
    try:
        response = _post(ctx, url, payload, token=ctx.thesis_token)
    except TransportError as error:
        return StageResult(
            STAGE_BACKTEST,
            False,
            f"backtest launch route unreachable ({error})",
            REMEDIATIONS[STAGE_BACKTEST],
        )
    if response.status_code != 200:
        return StageResult(
            STAGE_BACKTEST,
            False,
            f"backtest launch returned HTTP {response.status_code}",
            REMEDIATIONS[STAGE_BACKTEST],
        )
    body = response.body or {}
    job_id = body.get("job_id")
    if isinstance(job_id, str) and job_id:
        return StageResult(
            STAGE_BACKTEST, True, f"backtest launch acknowledged (job_id={job_id})"
        )
    exit_code = body.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return StageResult(
            STAGE_BACKTEST,
            True,
            "backtest launch delegated through the harness boundary "
            f"(exit_code={exit_code}); job_id is populated only by the "
            "remote harness API",
        )
    return StageResult(
        STAGE_BACKTEST,
        False,
        "backtest launch returned 200 without a job id or process exit code",
        REMEDIATIONS[STAGE_BACKTEST],
    )


def run_stages(
    ctx: StackContext, *, skip_chat: bool = False, skip_backtest: bool = False
) -> tuple[StageResult, ...]:
    """Run every enabled stage and return the results in execution order."""
    results = [check_harness(ctx), check_thesis(ctx), check_workspace(ctx)]
    if not skip_chat:
        results.append(check_chat(ctx))
    if not skip_backtest:
        results.append(check_backtest(ctx))
    return tuple(results)


def exit_code(results: Sequence[StageResult]) -> int:
    """Return 0 only when at least one stage ran and every stage passed."""
    if not results:
        return 1
    return 0 if all(result.passed for result in results) else 1


def render_report(results: Sequence[StageResult], redactor: SecretRedactor) -> str:
    """Render per-stage lines with secrets masked; never expose a token value."""
    lines: list[str] = []
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(f"[{mark}] {result.stage}: {redactor.apply(result.detail)}")
        if not result.passed and result.remediation:
            lines.append(f"       fix: {redactor.apply(result.remediation)}")
    passed = sum(1 for result in results if result.passed)
    lines.append(f"stages: {passed}/{len(results)} passed")
    return "\n".join(lines)


def remediation_for(stage: str) -> str:
    """Return the base remediation hint registered for a stage."""
    return REMEDIATIONS.get(stage, "")


def build_parser() -> argparse.ArgumentParser:
    """Build the verification CLI."""
    parser = argparse.ArgumentParser(
        prog="run_local_stack",
        description=(
            "Verify the local Thesis stack end to end: harness quant API, "
            "Thesis API auth boundary, workspace catalog, chat message, and "
            "harness-delegated backtest launch."
        ),
    )
    parser.add_argument(
        "--thesis-url",
        default=DEFAULT_THESIS_URL,
        help=f"Thesis API base URL (default {DEFAULT_THESIS_URL})",
    )
    parser.add_argument(
        "--harness-url",
        default=DEFAULT_HARNESS_URL,
        help=f"harness quant API base URL (default {DEFAULT_HARNESS_URL})",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "bearer token for both APIs; defaults to "
            f"{THESIS_TOKEN_ENV} (or {THESIS_TOKEN_ENV_SERVER}) for Thesis and "
            f"{HARNESS_TOKEN_ENV} for the harness API"
        ),
    )
    parser.add_argument(
        "--workspace",
        default=DEFAULT_WORKSPACE,
        help=f"workspace id to verify (default {DEFAULT_WORKSPACE})",
    )
    parser.add_argument(
        "--bundle-ref",
        default=DEFAULT_BUNDLE_REF,
        help=f"bundle reference for the backtest launch (default {DEFAULT_BUNDLE_REF})",
    )
    parser.add_argument(
        "--config-path",
        default=DEFAULT_CONFIG_PATH,
        help=f"harness config path for the backtest launch (default {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="skip the chat message stage",
    )
    parser.add_argument(
        "--skip-backtest",
        action="store_true",
        help="skip the backtest launch stage",
    )
    return parser


def resolve_tokens(token_override: str | None) -> tuple[str | None, str | None]:
    """Resolve the Thesis and harness bearer tokens for one run.

    The override applies to both APIs.  Otherwise the Thesis token comes from
    ``THESIS_API_BEARER_TOKEN`` (or the server-side ``THESIS_API__BEARER_TOKEN``
    spelling) and the harness token from ``HARNESS_API_BEARER_TOKEN``.  Values
    are never logged.
    """
    if token_override:
        return token_override, token_override
    thesis = (
        os.environ.get(THESIS_TOKEN_ENV)
        or os.environ.get(THESIS_TOKEN_ENV_SERVER)
        or None
    )
    harness = os.environ.get(HARNESS_TOKEN_ENV) or None
    return thesis, harness


def main(argv: Sequence[str] | None = None) -> int:
    """Run every enabled stage and print the redacted report."""
    args = build_parser().parse_args(argv)
    thesis_token, harness_token = resolve_tokens(args.token)
    redactor = SecretRedactor((thesis_token, harness_token, args.token))
    transport = HttpxTransport(args.timeout)
    try:
        ctx = StackContext(
            transport=transport,
            thesis_url=args.thesis_url.rstrip("/"),
            harness_url=args.harness_url.rstrip("/"),
            thesis_token=thesis_token,
            harness_token=harness_token,
            workspace=args.workspace,
            bundle_ref=args.bundle_ref,
            config_path=args.config_path,
        )
        results = run_stages(
            ctx, skip_chat=args.skip_chat, skip_backtest=args.skip_backtest
        )
    finally:
        transport.close()
    print(render_report(results, redactor))
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
