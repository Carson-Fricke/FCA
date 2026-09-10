"""Standalone screening worker: stdlib-only executor for one filter batch.

This module is the process-isolation boundary for custom screener filters.
It is deliberately importable without the ``harness`` package: the host
spawns it as a file (``python -I -S -B _worker.py``) and speaks the JSON
protocol below over stdin/stdout, so the worker imports only the standard
library and preloads only the allowlisted modules (``math``, ``statistics``).

Sandbox honesty (Windows).  The standard library offers no way to cap a
child process's memory on Windows, and AST-level blocking of
allocation-heavy patterns is NOT reliable (``[0] * n``, string repetition,
and loops are indistinguishable from legitimate screening code).  The
enforced controls are:

- import lock: only ``math`` and ``statistics`` resolve, rejected at AST
  validation time before any code runs, and again by the restricted
  ``__import__`` at runtime;
- restricted builtins: no ``open``, ``eval``, ``exec``, ``getattr``,
  ``vars``, ``globals``, ``input``, ``breakpoint``, ...;
- dunder lock: no dunder attribute or dunder name access anywhere in the
  source, which removes the ``__builtins__``/``__class__`` escape hatches;
- no wall clock, no networking, no subprocesses: the modules that provide
  them are outside the import allowlist and their attribute chains are
  rejected statically;
- wall-clock kill: the host enforces the batch timeout by terminating this
  process; there is no cooperative cancellation to escape.

Residual risk, stated plainly: a script that allocates in a loop can
exhaust host RAM before the wall-clock kill fires, because no OS-level
memory cap is available on Windows through the standard library.  Screen
only scripts from trusted authors, or run on a resource-isolated machine.

Request (one JSON document on stdin)::

    {"protocol": "fca-harness-screening-worker-v1",
     "script": "<python source>",
     "contexts": [{"facts": {...}, "bars": [...], "indicators": {...}}]}

Response (one JSON document on stdout)::

    {"protocol": ..., "ok": true, "script_hash": "<hex>",
     "results": {"AAPL": true, "MSFT": false}}
    {"protocol": ..., "ok": false, "script_hash": "<hex>",
     "error": {"kind": "import-blocked", "message": "...",
               "script_hash": "<hex>"}}

Script contract: the source defines exactly one ``def screen(ctx)``.
``ctx`` is a plain dict with ``facts`` (symbol, security_type, exchange,
sic_industry, as_of, fundamentals), ``bars`` (list of OHLCV dicts), and
``indicators`` (name -> list of floats or ``None`` aligned with ``bars``).
It returns a bool or a dict of ``symbol -> bool``; anything else is an
output-shape violation.  Cold start of this worker is dominated by
interpreter startup and measures roughly 0.1-0.3 s on a typical Windows
dev box (see ``tests/harness/screening/test_cold_start.py``).
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import math
import statistics
import sys
from collections.abc import Callable
from typing import cast

PROTOCOL_ID: str = "fca-harness-screening-worker-v1"
ALLOWED_MODULES: tuple[str, ...] = ("math", "statistics")
SCREEN_FUNCTION: str = "screen"
SCRIPT_HASH_DOMAIN: bytes = b"fca:harness-screening-script:v1\0"
MAX_PAYLOAD_BYTES: int = 64 * 1024 * 1024
MAX_MESSAGE_CHARS: int = 2000
DEFAULT_FILENAME: str = "<custom-screener>"

KIND_SYNTAX_ERROR: str = "syntax-error"
KIND_STRUCTURE_VIOLATION: str = "structure-violation"
KIND_IMPORT_BLOCKED: str = "import-blocked"
KIND_DUNDER_ACCESS: str = "dunder-access"
KIND_DANGEROUS_CALL: str = "dangerous-call"
KIND_WALL_CLOCK: str = "wall-clock"
KIND_SLEEPING: str = "sleeping"
KIND_NETWORKING: str = "networking"
KIND_SUBPROCESS: str = "subprocess"
KIND_CONCURRENCY: str = "concurrency"
KIND_ASYNC_SYNTAX: str = "async-syntax"
KIND_OUTPUT_SHAPE: str = "output-shape"
KIND_SCRIPT_ERROR: str = "script-error"
KIND_TIMEOUT: str = "timeout"
KIND_WORKER_FAILURE: str = "worker-failure"
KIND_INPUT_INVALID: str = "input-invalid"

_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "abs",
    "bool",
    "dict",
    "enumerate",
    "float",
    "frozenset",
    "int",
    "isinstance",
    "len",
    "list",
    "map",
    "max",
    "min",
    "range",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
)

_SAFE_BUILTINS: dict[str, object] = {
    name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES
}

_DANGEROUS_CALLS: frozenset[str] = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "input",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "breakpoint",
        "help",
        "super",
        "type",
        "object",
        "exit",
        "quit",
        "license",
        "credits",
    }
)

_DENIED_ATTRIBUTE_ROOTS: dict[str, str] = {
    "datetime": KIND_WALL_CLOCK,
    "time": KIND_WALL_CLOCK,
    "http": KIND_NETWORKING,
    "ftplib": KIND_NETWORKING,
    "httpx": KIND_NETWORKING,
    "imaplib": KIND_NETWORKING,
    "nntplib": KIND_NETWORKING,
    "poplib": KIND_NETWORKING,
    "requests": KIND_NETWORKING,
    "smtplib": KIND_NETWORKING,
    "socket": KIND_NETWORKING,
    "socketserver": KIND_NETWORKING,
    "ssl": KIND_NETWORKING,
    "telnetlib": KIND_NETWORKING,
    "urllib": KIND_NETWORKING,
    "websockets": KIND_NETWORKING,
    "xmlrpc": KIND_NETWORKING,
    "os": KIND_SUBPROCESS,
    "pty": KIND_SUBPROCESS,
    "subprocess": KIND_SUBPROCESS,
    "_thread": KIND_CONCURRENCY,
    "asyncio": KIND_CONCURRENCY,
    "concurrent": KIND_CONCURRENCY,
    "curio": KIND_CONCURRENCY,
    "multiprocessing": KIND_CONCURRENCY,
    "threading": KIND_CONCURRENCY,
    "trio": KIND_CONCURRENCY,
    "builtins": KIND_IMPORT_BLOCKED,
    "ctypes": KIND_IMPORT_BLOCKED,
    "glob": KIND_IMPORT_BLOCKED,
    "importlib": KIND_IMPORT_BLOCKED,
    "io": KIND_IMPORT_BLOCKED,
    "pathlib": KIND_IMPORT_BLOCKED,
    "pickle": KIND_IMPORT_BLOCKED,
    "platform": KIND_IMPORT_BLOCKED,
    "random": KIND_IMPORT_BLOCKED,
    "resource": KIND_IMPORT_BLOCKED,
    "secrets": KIND_IMPORT_BLOCKED,
    "shutil": KIND_IMPORT_BLOCKED,
    "signal": KIND_IMPORT_BLOCKED,
    "sys": KIND_IMPORT_BLOCKED,
    "tempfile": KIND_IMPORT_BLOCKED,
    "webbrowser": KIND_IMPORT_BLOCKED,
}

_RULE_REMEDIATIONS: dict[str, str] = {
    KIND_SYNTAX_ERROR: "fix the Python syntax error in the script",
    KIND_STRUCTURE_VIOLATION: (
        "define exactly one top-level screen(ctx) function and keep imports "
        "at the top of the module"
    ),
    KIND_IMPORT_BLOCKED: (
        "rely on the preloaded math and statistics modules and the data in ctx"
    ),
    KIND_DUNDER_ACCESS: (
        "operate on the plain data exposed through ctx without reflection"
    ),
    KIND_DANGEROUS_CALL: (
        "compute from ctx data with the restricted builtins provided"
    ),
    KIND_WALL_CLOCK: (
        "use only the as-of data provided in ctx; the sandbox has no clock"
    ),
    KIND_SLEEPING: "never sleep; evaluate the provided data and return",
    KIND_NETWORKING: "use only the data provided in ctx",
    KIND_SUBPROCESS: "use only the data provided in ctx",
    KIND_CONCURRENCY: "evaluate sequentially over the provided data",
    KIND_ASYNC_SYNTAX: "keep the screen function synchronous",
    KIND_OUTPUT_SHAPE: "return a bool or a dict of symbol -> bool",
    KIND_SCRIPT_ERROR: "handle missing or short series before computing",
    KIND_TIMEOUT: "reduce the work the screen function performs per symbol",
    KIND_WORKER_FAILURE: "retry once, then treat the screener as failing closed",
    KIND_INPUT_INVALID: "build the context through harness.screening APIs",
}


class _ScreenFailure(Exception):
    """Internal fail-closed signal carrying a structured error kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def script_hash(source: str) -> str:
    """Return the domain-separated content hash of the script source."""
    return hashlib.sha256(SCRIPT_HASH_DOMAIN + source.encode("utf-8")).hexdigest()


def _truncate(message: str) -> str:
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return message[: MAX_MESSAGE_CHARS - 3] + "..."


def _violation(
    kind: str,
    code: str,
    message: str,
    node: ast.AST | None = None,
    symbol: str = "",
) -> dict[str, object]:
    lineno = int(getattr(node, "lineno", 1)) if node is not None else 1
    col = int(getattr(node, "col_offset", 0)) if node is not None else 0
    remediation = _RULE_REMEDIATIONS.get(kind, "rewrite the script inside the sandbox")
    return {
        "code": code,
        "kind": kind,
        "message": _truncate(message),
        "remediation": remediation,
        "symbol": symbol,
        "lineno": lineno,
        "col_offset": col,
    }


def _violation_order(record: dict[str, object]) -> tuple[int, int, str]:
    lineno = record["lineno"]
    col = record["col_offset"]
    code = record["code"]
    return (
        lineno if isinstance(lineno, int) else 0,
        col if isinstance(col, int) else 0,
        code if isinstance(code, str) else "",
    )


def _dotted_parts(expression: ast.expr) -> list[str] | None:
    """Return the dotted name of a pure name/attribute chain.

    A chain passing through a call result is not a dotted module path, so
    ``math.sqrt(2).bit_length()``-style chains stop at the call boundary.
    """
    parts: list[str] = []
    current: ast.expr = expression
    while True:
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
            continue
        if isinstance(current, ast.Name):
            parts.append(current.id)
            parts.reverse()
            return parts
        return None


def _import_violations(
    statement: ast.Import | ast.ImportFrom,
) -> list[dict[str, object]]:
    """Return the allowlist violations for one import statement.

    Runs for every import node in the tree, so imports nested inside the
    screen function are rejected statically exactly like top-level ones.
    """
    violations: list[dict[str, object]] = []
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            if alias.name not in ALLOWED_MODULES:
                violations.append(
                    _violation(
                        KIND_IMPORT_BLOCKED,
                        "HS100",
                        f"import of {alias.name!r} is outside the screening allowlist",
                        statement,
                        symbol=alias.name,
                    )
                )
            elif alias.asname is not None:
                violations.append(
                    _violation(
                        KIND_IMPORT_BLOCKED,
                        "HS101",
                        "import aliases are not permitted in screening scripts",
                        statement,
                        symbol=alias.name,
                    )
                )
        return violations
    if statement.level:
        violations.append(
            _violation(
                KIND_IMPORT_BLOCKED,
                "HS102",
                "relative imports are not permitted in screening scripts",
                statement,
            )
        )
        return violations
    if statement.module not in ALLOWED_MODULES:
        violations.append(
            _violation(
                KIND_IMPORT_BLOCKED,
                "HS100",
                f"import from {statement.module!r} is outside the screening allowlist",
                statement,
                symbol=statement.module or "",
            )
        )
        return violations
    for alias in statement.names:
        if alias.name == "*":
            violations.append(
                _violation(
                    KIND_IMPORT_BLOCKED,
                    "HS103",
                    "star imports are not permitted in screening scripts",
                    statement,
                )
            )
        elif alias.asname is not None:
            violations.append(
                _violation(
                    KIND_IMPORT_BLOCKED,
                    "HS101",
                    "import aliases are not permitted in screening scripts",
                    statement,
                    symbol=alias.name,
                )
            )
    return violations


def _structure_violations(tree: ast.Module) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    screen: ast.FunctionDef | None = None
    for statement in tree.body:
        if isinstance(statement, ast.Import | ast.ImportFrom):
            continue
        if isinstance(statement, ast.AsyncFunctionDef):
            violations.append(
                _violation(
                    KIND_ASYNC_SYNTAX,
                    "HS700",
                    "async syntax is not permitted in screening scripts",
                    statement,
                    symbol=statement.name,
                )
            )
            continue
        if isinstance(statement, ast.FunctionDef):
            if screen is not None or statement.name != SCREEN_FUNCTION:
                violations.append(
                    _violation(
                        KIND_STRUCTURE_VIOLATION,
                        "HS011",
                        "the module must define exactly one screen(ctx) function",
                        statement,
                        symbol=statement.name,
                    )
                )
            else:
                screen = statement
            continue
        violations.append(
            _violation(
                KIND_STRUCTURE_VIOLATION,
                "HS012",
                f"top-level {type(statement).__name__} is not permitted in a"
                " screening script",
                statement,
            )
        )
    if screen is None:
        missing = _violation(
            KIND_STRUCTURE_VIOLATION,
            "HS013",
            f"the module must define a {SCREEN_FUNCTION}(ctx) function",
            tree,
        )
        missing["lineno"] = int(tree.body[-1].lineno) + 1 if tree.body else 1
        violations.append(missing)
    return violations


def _expression_violations(tree: ast.Module) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            violations.extend(_import_violations(node))
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                violations.append(
                    _violation(
                        KIND_DUNDER_ACCESS,
                        "HS200",
                        f"dunder attribute access to {node.attr!r} is not permitted",
                        node,
                        symbol=node.attr,
                    )
                )
                continue
            parts = _dotted_parts(node)
            if parts is None or len(parts) < 2:
                continue
            root = parts[0]
            attr = parts[-1]
            if root == "time" and attr == "sleep":
                violations.append(
                    _violation(
                        KIND_SLEEPING,
                        "HS401",
                        "sleeping is not permitted in screening scripts",
                        node,
                        symbol=".".join(parts),
                    )
                )
                continue
            denied = _DENIED_ATTRIBUTE_ROOTS.get(root)
            if denied is not None:
                violations.append(
                    _violation(
                        denied,
                        "HS3xx",
                        f"access through {root!r} escapes the screening sandbox",
                        node,
                        symbol=".".join(parts),
                    )
                )
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                violations.append(
                    _violation(
                        KIND_DUNDER_ACCESS,
                        "HS201",
                        f"reference to dunder name {node.id!r} is not permitted",
                        node,
                        symbol=node.id,
                    )
                )
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in _DANGEROUS_CALLS:
                violations.append(
                    _violation(
                        KIND_DANGEROUS_CALL,
                        "HS300",
                        f"call to {function.id!r} is not permitted in screening"
                        " scripts",
                        node,
                        symbol=function.id,
                    )
                )
        elif isinstance(node, ast.ClassDef):
            violations.append(
                _violation(
                    KIND_STRUCTURE_VIOLATION,
                    "HS014",
                    "class definitions are not permitted in screening scripts",
                    node,
                    symbol=node.name,
                )
            )
        elif isinstance(
            node, ast.AsyncFunctionDef | ast.Await | ast.AsyncFor | ast.AsyncWith
        ):
            violations.append(
                _violation(
                    KIND_ASYNC_SYNTAX,
                    "HS700",
                    "async syntax is not permitted in screening scripts",
                    node,
                )
            )
        elif isinstance(node, ast.comprehension) and node.is_async:
            violations.append(
                _violation(
                    KIND_ASYNC_SYNTAX,
                    "HS700",
                    "async syntax is not permitted in screening scripts",
                    node,
                )
            )
    return violations


def validate_source(source: object) -> list[dict[str, object]]:
    """Return every AST-level boundary violation in ``source``.

    The validator is static and side-effect free: it parses text, never
    imports or executes script code, and never reads the clock, the
    network, or process-global state.
    """
    if not isinstance(source, str):
        return [
            _violation(
                KIND_STRUCTURE_VIOLATION,
                "HS010",
                "screener source must be a non-empty string",
                None,
            )
        ]
    if not source.strip():
        return [
            _violation(
                KIND_STRUCTURE_VIOLATION,
                "HS013",
                f"the module must define a {SCREEN_FUNCTION}(ctx) function",
                None,
            )
        ]
    try:
        tree = ast.parse(source, filename=DEFAULT_FILENAME, mode="exec")
    except (SyntaxError, ValueError) as error:
        return [
            _violation(
                KIND_SYNTAX_ERROR,
                "HS001",
                f"screener source does not parse: {error}",
                None,
            )
        ]
    violations = [*_structure_violations(tree), *_expression_violations(tree)]
    return sorted(violations, key=_violation_order)


def _restricted_import(name: str, *args: object, **kwargs: object) -> object:
    """Import boundary: only the preloaded allowlisted modules resolve."""
    if name not in ALLOWED_MODULES:
        raise ImportError(f"import of {name!r} is blocked in screening scripts")
    return {"math": math, "statistics": statistics}[name]


def build_namespace() -> dict[str, object]:
    """Build the restricted globals a screening script executes against."""
    builtins_map: dict[str, object] = dict(_SAFE_BUILTINS)
    builtins_map["__import__"] = _restricted_import
    return {"__builtins__": builtins_map, "math": math, "statistics": statistics}


def _normalized_decision(value: object, symbol: str) -> dict[str, bool]:
    """Normalize one screen() return value into ``symbol -> bool`` entries."""
    if isinstance(value, bool):
        return {symbol: value}
    if isinstance(value, dict):
        if not value:
            raise _ScreenFailure(
                KIND_OUTPUT_SHAPE,
                "screen returned an empty decision mapping; it must decide the"
                f" current symbol {symbol!r}",
            )
        for key, passed in value.items():
            if not isinstance(key, str) or not key:
                raise _ScreenFailure(
                    KIND_OUTPUT_SHAPE,
                    "screen decision keys must be non-empty symbol strings",
                )
            if not isinstance(passed, bool):
                raise _ScreenFailure(
                    KIND_OUTPUT_SHAPE,
                    f"screen decision for {key!r} must be a bool",
                )
        if symbol not in value:
            raise _ScreenFailure(
                KIND_OUTPUT_SHAPE,
                f"screen decision mapping must include the current symbol {symbol!r}",
            )
        return value
    raise _ScreenFailure(
        KIND_OUTPUT_SHAPE,
        "screen must return a bool or a dict of symbol -> bool; got"
        f" {type(value).__name__}",
    )


def _payload_symbols(payloads: list[dict[str, object]]) -> list[str]:
    """Extract and validate the symbol of every context payload."""
    symbols: list[str] = []
    for payload in payloads:
        facts = payload.get("facts")
        symbol = facts.get("symbol") if isinstance(facts, dict) else None
        if not isinstance(symbol, str) or not symbol:
            raise _ScreenFailure(
                KIND_INPUT_INVALID,
                "each context payload must carry a non-empty facts.symbol",
            )
        symbols.append(symbol)
    return symbols


def collect_decisions(
    screen: Callable[[dict[str, object]], object],
    payloads: list[dict[str, object]],
) -> dict[str, bool]:
    """Call ``screen`` once per context payload and merge its decisions."""
    symbols = _payload_symbols(payloads)
    known = set(symbols)
    decided: dict[str, bool] = {}
    for payload, symbol in zip(payloads, symbols, strict=True):
        try:
            value = screen(payload)
        except _ScreenFailure:
            raise
        except Exception as error:  # noqa: BLE001 - script errors fail closed
            raise _ScreenFailure(
                KIND_SCRIPT_ERROR,
                f"screen failed for {symbol!r}: {type(error).__name__}: {error}",
            ) from error
        for key, passed in _normalized_decision(value, symbol).items():
            if key not in known:
                raise _ScreenFailure(
                    KIND_OUTPUT_SHAPE,
                    f"screen decided unknown symbol {key!r}",
                )
            if key in decided:
                raise _ScreenFailure(
                    KIND_OUTPUT_SHAPE,
                    f"symbol {key!r} was decided more than once",
                )
            decided[key] = passed
    missing = [symbol for symbol in symbols if symbol not in decided]
    if missing:
        raise _ScreenFailure(
            KIND_OUTPUT_SHAPE,
            f"no decision for symbol(s) {missing}",
        )
    return decided


def execute_program(source: str, payloads: list[dict[str, object]]) -> dict[str, bool]:
    """Validate, compile, and execute the screen program over the payloads."""
    violations = validate_source(source)
    if violations:
        first = violations[0]
        rendered = (
            f"{first['code']} [line {first['lineno']}] {first['message']}"
            f" (fix: {first['remediation']})"
        )
        if len(violations) > 1:
            rendered += f"; {len(violations) - 1} more violation(s)"
        raise _ScreenFailure(str(first["kind"]), rendered)
    try:
        tree = ast.parse(source, filename=DEFAULT_FILENAME, mode="exec")
        code = compile(tree, filename=DEFAULT_FILENAME, mode="exec")
    except (SyntaxError, ValueError) as error:
        raise _ScreenFailure(
            KIND_SYNTAX_ERROR, f"screener source does not parse: {error}"
        ) from error
    namespace = build_namespace()
    try:
        exec(code, namespace)  # noqa: S102 - restricted-globals boundary
    except Exception as error:  # noqa: BLE001 - module-level failures fail closed
        raise _ScreenFailure(
            KIND_SCRIPT_ERROR,
            f"screen module failed to initialize: {type(error).__name__}: {error}",
        ) from error
    screen = namespace.get(SCREEN_FUNCTION)
    if not callable(screen):
        raise _ScreenFailure(
            KIND_OUTPUT_SHAPE,
            f"{SCREEN_FUNCTION} must be a callable function",
        )
    return collect_decisions(
        cast("Callable[[dict[str, object]], object]", screen), payloads
    )


def _error_response(kind: str, message: str, script_hash: str) -> dict[str, object]:
    return {
        "protocol": PROTOCOL_ID,
        "ok": False,
        "script_hash": script_hash,
        "error": {
            "kind": kind,
            "message": _truncate(message),
            "script_hash": script_hash,
        },
    }


def _handle_request(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        return _error_response(KIND_INPUT_INVALID, "request must be a JSON object", "")
    if request.get("protocol") != PROTOCOL_ID:
        return _error_response(
            KIND_INPUT_INVALID, "request protocol does not match the worker", ""
        )
    script = request.get("script")
    contexts = request.get("contexts")
    if not isinstance(script, str):
        return _error_response(
            KIND_INPUT_INVALID, "request script must be a string", ""
        )
    if not isinstance(contexts, list) or not all(
        isinstance(context, dict) for context in contexts
    ):
        return _error_response(
            KIND_INPUT_INVALID,
            "request contexts must be a list of JSON objects",
            script_hash(script),
        )
    try:
        results = execute_program(script, contexts)
    except _ScreenFailure as failure:
        return _error_response(failure.kind, failure.message, script_hash(script))
    return {
        "protocol": PROTOCOL_ID,
        "ok": True,
        "script_hash": script_hash(script),
        "results": results,
    }


def main() -> int:
    """Read one request from stdin and write one JSON response to stdout."""
    response: dict[str, object]
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            response = _error_response(
                KIND_INPUT_INVALID, "request exceeds the worker payload cap", ""
            )
        else:
            request = json.loads(raw.decode("utf-8"))
            response = _handle_request(request)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        response = _error_response(
            KIND_INPUT_INVALID, f"request is not valid JSON: {error}", ""
        )
    except Exception as error:  # noqa: BLE001 - the worker always answers
        response = _error_response(
            KIND_WORKER_FAILURE,
            f"worker crashed: {type(error).__name__}: {error}",
            "",
        )
    sys.stdout.write(json.dumps(response, sort_keys=True, allow_nan=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
