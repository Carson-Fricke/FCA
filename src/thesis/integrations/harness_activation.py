"""Thesis activation adapters over the local and remote quant-harness boundaries.

Thesis asks the quant-harness to run a strategy bundle; the harness owns all
runtime logic.  This module only renders the shared activation request for the
two deployment boundaries of :meth:`harness activation`: the local
``harness`` process boundaries (``backtest-job --config <path>`` and
``trader --config <path>``) and the authenticated remote harness API
(``POST /v1/backtests/jobs`` under a bearer credential).

The adapters are consent-model-agnostic: an activation request is a plain
bundle reference, environment, action, and configuration path.  The separately
owned consent and gatekeeper layers compose on top of this contract.  Bearer
credentials are injected per call and are never stored on an adapter, so no
serialized adapter state can disclose one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from thesis.integrations.harness import ProcessResult

ActivationAction = Literal["backtest", "trader"]
ActivationEnvironment = Literal["paper", "shadow", "live"]
CapabilityName = Literal["backtest", "trader", "shadow", "live"]

_CAPABILITY_NAMES: tuple[CapabilityName, ...] = (
    "backtest",
    "trader",
    "shadow",
    "live",
)

_TRADER_CAPABILITY: Mapping[ActivationEnvironment, CapabilityName] = {
    "paper": "trader",
    "shadow": "shadow",
    "live": "live",
}


class HarnessActivationError(RuntimeError):
    """Base error for a failed or misused harness activation."""


class CapabilityUnavailableError(HarnessActivationError):
    """Raised before any invocation when the harness lacks the capability."""

    def __init__(self, capability: str, message: str | None = None) -> None:
        self.capability = capability
        super().__init__(
            message or f"quant-harness capability {capability!r} is unavailable"
        )


class HarnessActivationProtocolError(HarnessActivationError):
    """A harness boundary response violates the published contract."""


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """Fail-closed declaration of what the target harness actually supports.

    Backtest and paper-trader activation are available by default, matching the
    implemented local ``backtest-job`` and ``trader`` process boundaries.
    Shadow and live stay unavailable until the harness publishes their gates.
    """

    backtest: bool = True
    trader: bool = True
    shadow: bool = False
    live: bool = False

    def __post_init__(self) -> None:
        for name in _CAPABILITY_NAMES:
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"capability {name!r} must be a boolean")

    def available(self, capability: str) -> bool:
        """Return whether the named capability is declared available."""
        _require_known_capability(capability)
        return bool(getattr(self, capability))

    def require_capability(self, capability: str) -> None:
        """Raise :class:`CapabilityUnavailableError` unless declared available."""
        if not self.available(capability):
            raise CapabilityUnavailableError(capability)

    def as_mapping(self) -> Mapping[str, bool]:
        """Return the declaration keyed by capability name."""
        return {name: getattr(self, name) for name in _CAPABILITY_NAMES}


def _require_known_capability(capability: str) -> None:
    if capability not in _CAPABILITY_NAMES:
        raise ValueError(
            f"unknown harness capability {capability!r}; "
            f"known capabilities: {list(_CAPABILITY_NAMES)}"
        )


@dataclass(frozen=True, slots=True)
class HarnessActivationRequest:
    """The single request contract shared by local and remote boundaries.

    ``bundle_ref`` is the content-addressed strategy bundle identity (plain
    hash text; grant semantics live in the consent layer above this module).
    """

    bundle_ref: str
    config_path: str
    action: ActivationAction
    environment: ActivationEnvironment = "paper"

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_ref", _text(self.bundle_ref, "bundle_ref"))
        object.__setattr__(self, "config_path", _text(self.config_path, "config_path"))
        if self.action not in ("backtest", "trader"):
            raise ValueError(
                f"activation action must be 'backtest' or 'trader', got {self.action!r}"
            )
        if self.environment not in ("paper", "shadow", "live"):
            raise ValueError(
                "activation environment must be 'paper', 'shadow', or 'live', "
                f"got {self.environment!r}"
            )

    def to_payload(self) -> dict[str, object]:
        """Serialize the published activation fields, identically per boundary."""
        return {
            "schema_version": 1,
            "bundle_ref": self.bundle_ref,
            "environment": self.environment,
            "action": self.action,
            "config_path": self.config_path,
        }

    def required_capability(self) -> CapabilityName:
        """Return the harness capability this request needs."""
        if self.action == "backtest":
            return "backtest"
        return _TRADER_CAPABILITY[self.environment]


@dataclass(frozen=True, slots=True)
class HarnessApiResponse:
    """Decoded JSON response returned by an injected HTTP transport."""

    status_code: int
    body: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("status_code must be an integer")
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be the decoded JSON object mapping")


class HarnessApiTransport(Protocol):
    """HTTP request surface implemented by a real client or a test fake."""

    def get(self, url: str, headers: Mapping[str, str]) -> HarnessApiResponse: ...

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HarnessApiResponse: ...


class HarnessProcessRunner(Protocol):
    """Injected local process boundary; no subprocess is created here."""

    def __call__(self, argv: Sequence[str]) -> ProcessResult: ...


@dataclass(frozen=True, slots=True)
class ActivationOutcome:
    """Inert record of one activation invocation; Thesis never interprets it."""

    action: ActivationAction
    bundle_ref: str
    environment: ActivationEnvironment
    request_payload: Mapping[str, object]
    exit_code: int | None = None
    job_id: str | None = None
    state: str | None = None
    stdout: str = ""
    stderr: str = ""
    raw: Mapping[str, object] = field(default_factory=dict)


class HarnessBoundary(Protocol):
    """The seam between the local process and remote API harness deployments."""

    @property
    def capabilities(self) -> HarnessCapabilities: ...

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome: ...

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome: ...


class LocalHarnessProcess:
    """Activation boundary invoking the local ``harness`` process commands.

    The runner is injected: it receives the rendered argv for
    ``backtest-job --config <path>`` or ``trader --config <path>`` and returns
    a :class:`ProcessResult`.  Nothing about harness runtime behavior is
    constructed here; a non-zero exit code is reported as data, because the
    process exit code is the harness-observed outcome.
    """

    def __init__(
        self,
        runner: HarnessProcessRunner,
        *,
        program: Sequence[str] = ("harness",),
        capabilities: HarnessCapabilities | None = None,
    ) -> None:
        if not callable(runner):
            raise TypeError("runner must be a callable taking (argv)")
        program_tuple = tuple(program)
        if not program_tuple or any(
            not isinstance(part, str) or not part.strip() for part in program_tuple
        ):
            raise ValueError(
                "program must be a non-empty sequence of non-empty strings"
            )
        self._runner = runner
        self._program = program_tuple
        self._capabilities = (
            HarnessCapabilities() if capabilities is None else capabilities
        )

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        """Invoke the local ``backtest-job`` process for the request."""
        self._reject_credential(bearer_token)
        return self._invoke(request)

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        """Invoke the local ``trader`` process for the request."""
        self._reject_credential(bearer_token)
        return self._invoke(request)

    def _invoke(self, request: HarnessActivationRequest) -> ActivationOutcome:
        self._capabilities.require_capability(request.required_capability())
        argv = self._argv(request)
        try:
            result = self._runner(argv)
        except Exception as error:
            raise HarnessActivationError(
                f"the local harness process {argv[1]!r} could not be invoked"
            ) from error
        if not isinstance(result, ProcessResult):
            raise HarnessActivationProtocolError(
                "the local harness runner returned an invalid process result"
            )
        return ActivationOutcome(
            action=request.action,
            bundle_ref=request.bundle_ref,
            environment=request.environment,
            request_payload=request.to_payload(),
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _argv(self, request: HarnessActivationRequest) -> tuple[str, ...]:
        command = "backtest-job" if request.action == "backtest" else "trader"
        return (*self._program, command, "--config", request.config_path)

    @staticmethod
    def _reject_credential(bearer_token: str | None) -> None:
        if bearer_token is not None:
            raise HarnessActivationError(
                "local harness processes take credentials from the operator "
                "environment, never from Thesis"
            )


_BACKTEST_LAUNCH_ROUTE = "/v1/backtests/jobs"
_DEFAULT_REMOTE_ROUTES: Mapping[str, str] = {"backtest": _BACKTEST_LAUNCH_ROUTE}


class RemoteHarnessApi:
    """Activation boundary for the bearer-authenticated remote harness API.

    The credential is supplied per call and is used to build the
    ``Authorization`` header at request time; it is never stored on the
    adapter.  The remote quant API currently publishes only backtest launch
    and query routes, so trader activation is unavailable by default until
    the planned harness deployment publishes its route.
    """

    def __init__(
        self,
        base_url: str,
        transport: HarnessApiTransport,
        *,
        capabilities: HarnessCapabilities | None = None,
        routes: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not base_url.strip().lower().startswith("https://"):
            raise ValueError("remote harness base_url must use HTTPS")
        if not callable(getattr(transport, "get", None)) or not callable(
            getattr(transport, "post", None)
        ):
            raise TypeError("transport must implement HarnessApiTransport (get/post)")
        self._base_url = base_url.strip().rstrip("/")
        self._transport = transport
        self._routes: Mapping[str, str] = (
            dict(_DEFAULT_REMOTE_ROUTES) if routes is None else dict(routes)
        )
        self._capabilities = (
            HarnessCapabilities(backtest=True, trader=False)
            if capabilities is None
            else capabilities
        )

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        """Post the activation request to the published backtest launch route."""
        return self._invoke(request, bearer_token)

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        """Post the activation request to the configured trader route."""
        return self._invoke(request, bearer_token)

    def _invoke(
        self, request: HarnessActivationRequest, bearer_token: str | None
    ) -> ActivationOutcome:
        self._capabilities.require_capability(request.required_capability())
        route = self._routes.get(request.action)
        if route is None:
            raise HarnessActivationError(
                "the remote harness API does not publish a "
                f"{request.action!r} activation route"
            )
        headers = {"Authorization": f"Bearer {_require_bearer(bearer_token)}"}
        payload = request.to_payload()
        try:
            response = self._transport.post(
                f"{self._base_url}{route}", headers, payload
            )
        except Exception as error:
            raise HarnessActivationError(
                "the remote harness API could not be reached"
            ) from error
        return _accepted_outcome(request, response)


class ActivationAdapter:
    """Thesis-facing facade over either boundary with one request contract."""

    def __init__(self, boundary: HarnessBoundary) -> None:
        for name in ("run_backtest", "launch_trader"):
            if not callable(getattr(boundary, name, None)):
                raise TypeError(
                    f"boundary must implement HarnessBoundary ({name} is missing)"
                )
        if not isinstance(getattr(boundary, "capabilities", None), HarnessCapabilities):
            raise TypeError("boundary must expose a HarnessCapabilities record")
        self._boundary = boundary

    @property
    def capabilities(self) -> HarnessCapabilities:
        """Return the target harness capability declaration."""
        return self._boundary.capabilities

    def run_backtest(
        self,
        bundle_ref: str,
        config_path: str,
        *,
        environment: ActivationEnvironment = "paper",
        bearer_token: str | None = None,
    ) -> ActivationOutcome:
        """Ask the harness to run one deterministic backtest for the bundle."""
        request = HarnessActivationRequest(
            bundle_ref=bundle_ref,
            config_path=config_path,
            action="backtest",
            environment=environment,
        )
        return self._boundary.run_backtest(request, bearer_token=bearer_token)

    def launch_trader(
        self,
        bundle_ref: str,
        config_path: str,
        *,
        environment: ActivationEnvironment = "paper",
        bearer_token: str | None = None,
    ) -> ActivationOutcome:
        """Ask the harness to launch one supervised trader for the bundle."""
        request = HarnessActivationRequest(
            bundle_ref=bundle_ref,
            config_path=config_path,
            action="trader",
            environment=environment,
        )
        return self._boundary.launch_trader(request, bearer_token=bearer_token)


def _require_bearer(bearer_token: str | None) -> str:
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise HarnessActivationError(
            "a non-empty bearer credential is required for remote harness activation"
        )
    return bearer_token.strip()


def _accepted_outcome(
    request: HarnessActivationRequest, response: HarnessApiResponse
) -> ActivationOutcome:
    status_code = response.status_code
    if status_code in (401, 403):
        raise HarnessActivationError(
            "the remote harness rejected the activation credential "
            f"(HTTP {status_code})"
        )
    if status_code in (404, 501):
        raise CapabilityUnavailableError(
            request.required_capability(),
            f"the remote harness API does not implement {request.action!r} "
            f"activation (HTTP {status_code})",
        )
    if status_code < 200 or status_code >= 300:
        raise HarnessActivationError(
            f"remote harness activation failed (HTTP {status_code})"
        )
    body = response.body
    job_id = body.get("job_id")
    state = body.get("state")
    if not isinstance(job_id, str) or not job_id.strip():
        raise HarnessActivationProtocolError(
            "remote harness launch response requires a non-empty 'job_id' string"
        )
    if not isinstance(state, str) or not state.strip():
        raise HarnessActivationProtocolError(
            "remote harness launch response requires a non-empty 'state' string"
        )
    return ActivationOutcome(
        action=request.action,
        bundle_ref=request.bundle_ref,
        environment=request.environment,
        request_payload=request.to_payload(),
        job_id=job_id.strip(),
        state=state.strip(),
        raw=dict(body),
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


__all__ = [
    "ActivationAction",
    "ActivationAdapter",
    "ActivationEnvironment",
    "ActivationOutcome",
    "CapabilityName",
    "CapabilityUnavailableError",
    "HarnessActivationError",
    "HarnessActivationProtocolError",
    "HarnessActivationRequest",
    "HarnessApiTransport",
    "HarnessApiResponse",
    "HarnessBoundary",
    "HarnessCapabilities",
    "HarnessProcessRunner",
    "LocalHarnessProcess",
    "RemoteHarnessApi",
]
