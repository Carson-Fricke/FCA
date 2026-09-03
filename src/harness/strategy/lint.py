"""AST-based sandbox lint for strategy sources.

A strategy runs inside a deterministic kernel: it observes time through
``context.clock``, schedules work through ``context.scheduler``, draws
randomness from ``context.random``, reads history through ``context.data``, and
emits commands through ``context.orders``.  This module statically inspects
strategy source with the standard-library :mod:`ast` module and reports every
construct that escapes those boundaries:

* wall-clock reads (``time.time``, ``datetime.now``, ``os.times``)
* sleeping (``time.sleep``)
* networking (``socket``, ``urllib``, ``httpx``, ``requests``, ...)
* subprocesses (``subprocess``, ``pty``, ``os.system``, ``os.spawn*``)
* unseeded randomness (``random.random``, ``secrets``, ``os.urandom``)
* asyncio and async syntax (``asyncio``, ``async def``, ``await``)
* concurrency (``threading``, ``multiprocessing``, ``concurrent.futures``)

The checker is static and side-effect free.  It parses text, never imports or
executes strategy code, and never reads the clock, the network, or process-global
randomness, so it is safe to run inside the test suite and in CI::

    from harness.strategy.lint import assert_sandbox_clean, lint_strategy

    report = lint_strategy("strategies/harness_examples/example.py")
    assert report.ok, report.render()

Two resolution rules keep the checker predictable:

* Prohibited *modules* are reported once, at their import statement.
* Prohibited *attributes* are reported at every use.  An attribute chain that
  passes through a call result (``random.Random(7).random()``) is treated as a
  method on a returned object rather than a module attribute, so seeded
  generators and context boundaries stay legal.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType

__all__ = [
    "ASYNC_SYNTAX_RULE",
    "DEFAULT_FILENAME",
    "RULES",
    "LintReport",
    "SandboxRule",
    "SandboxViolation",
    "SandboxViolationError",
    "StrategyLintError",
    "StrategyTarget",
    "ViolationCategory",
    "assert_sandbox_clean",
    "iter_strategy_files",
    "lint_strategy",
    "lint_strategy_class",
    "lint_strategy_directory",
    "lint_strategy_file",
    "lint_strategy_module",
    "lint_strategy_source",
]

DEFAULT_FILENAME = "<strategy>"


class ViolationCategory(StrEnum):
    """One class of determinism-breaking strategy behavior."""

    WALL_CLOCK = "wall-clock"
    SLEEPING = "sleeping"
    NETWORKING = "networking"
    SUBPROCESS = "subprocess"
    UNSEEDED_RANDOM = "unseeded-random"
    ASYNCIO = "asyncio"
    CONCURRENCY = "concurrency"


class StrategyLintError(RuntimeError):
    """Raised when a strategy target cannot be linted."""


@dataclass(frozen=True, slots=True)
class SandboxRule:
    """One prohibited module or module attribute.

    ``attributes=None`` prohibits the whole module; otherwise only the listed
    attribute names are prohibited while the module itself stays legal (for
    example ``time.sleep`` versus ``time.timezone``).
    """

    code: str
    category: ViolationCategory
    module: str
    attributes: frozenset[str] | None
    summary: str
    remediation: str


@dataclass(frozen=True, slots=True)
class SandboxViolation:
    """One actionable sandbox diagnostic."""

    filename: str
    lineno: int
    col_offset: int
    code: str
    category: ViolationCategory
    symbol: str
    message: str
    remediation: str

    @property
    def location(self) -> str:
        """Return the ``file:line:column`` position of this violation."""
        return f"{self.filename}:{self.lineno}:{self.col_offset}"

    def format(self) -> str:
        """Render one diagnostic line naming the rule and its remedy."""
        return (
            f"{self.location}: {self.code} [{self.category}] {self.message}"
            f" (fix: {self.remediation})"
        )


@dataclass(frozen=True, slots=True)
class LintReport:
    """All sandbox violations found in one linted target."""

    filename: str
    violations: tuple[SandboxViolation, ...]

    @property
    def ok(self) -> bool:
        """Return whether the target stays inside the sandbox."""
        return not self.violations

    @property
    def codes(self) -> tuple[str, ...]:
        """Return the rule code of every violation in report order."""
        return tuple(violation.code for violation in self.violations)

    @property
    def categories(self) -> frozenset[ViolationCategory]:
        """Return the categories present in this report."""
        return frozenset(violation.category for violation in self.violations)

    def matching(
        self,
        category: ViolationCategory | None = None,
        code: str | None = None,
    ) -> tuple[SandboxViolation, ...]:
        """Return violations filtered by category and/or rule code."""
        return tuple(
            violation
            for violation in self.violations
            if (category is None or violation.category is category)
            and (code is None or violation.code == code)
        )

    def render(self) -> str:
        """Render a human-readable report for console output."""
        if not self.violations:
            return f"{self.filename}: strategy sandbox clean"
        header = f"{self.filename}: {len(self.violations)} sandbox violation(s)"
        return "\n".join([header, *(f"  {v.format()}" for v in self.violations)])

    def raise_for_status(self) -> None:
        """Raise :class:`SandboxViolationError` when the target is not clean."""
        if not self.ok:
            raise SandboxViolationError(self)


class SandboxViolationError(StrategyLintError):
    """Raised when a strategy violates the sandbox contract."""

    def __init__(self, report: LintReport) -> None:
        super().__init__(report.render())
        self.report = report


_NETWORK_MODULES: tuple[str, ...] = (
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "imaplib",
    "nntplib",
    "poplib",
    "requests",
    "smtplib",
    "socket",
    "socketserver",
    "ssl",
    "telnetlib",
    "urllib",
    "websockets",
    "xmlrpc",
)

_ASYNC_MODULES: tuple[str, ...] = ("anyio", "asyncio", "curio", "trio")

_CONCURRENCY_MODULES: tuple[str, ...] = (
    "_thread",
    "concurrent",
    "eventlet",
    "gevent",
    "multiprocessing",
    "threading",
)


def _network_rules() -> tuple[SandboxRule, ...]:
    return tuple(
        SandboxRule(
            code=f"SB3{index:02d}",
            category=ViolationCategory.NETWORKING,
            module=module,
            attributes=None,
            summary=(
                f"performs network I/O through the {module} module, which the"
                " kernel cannot order or replay"
            ),
            remediation=(
                "read market data through context.data and context.subscriptions"
                " instead of opening a connection"
            ),
        )
        for index, module in enumerate(_NETWORK_MODULES)
    )


def _async_rules() -> tuple[SandboxRule, ...]:
    return tuple(
        SandboxRule(
            code=f"SB5{index:02d}",
            category=ViolationCategory.ASYNCIO,
            module=module,
            attributes=None,
            summary=(
                f"drives an asynchronous event loop ({module}) inside the"
                " synchronous kernel"
            ),
            remediation=(
                "keep callbacks synchronous and schedule timestamped events"
                " through context.scheduler"
            ),
        )
        for index, module in enumerate(_ASYNC_MODULES)
    )


def _concurrency_rules() -> tuple[SandboxRule, ...]:
    return tuple(
        SandboxRule(
            code=f"SB6{index:02d}",
            category=ViolationCategory.CONCURRENCY,
            module=module,
            attributes=None,
            summary=(
                f"runs work concurrently through {module}, whose ordering the"
                " kernel cannot replay"
            ),
            remediation=(
                "order work deterministically through context.scheduler and the"
                " event bus"
            ),
        )
        for index, module in enumerate(_CONCURRENCY_MODULES)
    )


RULES: tuple[SandboxRule, ...] = (
    SandboxRule(
        code="SB001",
        category=ViolationCategory.WALL_CLOCK,
        module="time",
        attributes=frozenset(
            {
                "clock_gettime",
                "clock_gettime_ns",
                "ctime",
                "gmtime",
                "localtime",
                "monotonic",
                "monotonic_ns",
                "perf_counter",
                "perf_counter_ns",
                "process_time",
                "process_time_ns",
                "thread_time",
                "thread_time_ns",
                "time",
                "time_ns",
            }
        ),
        summary=("reads the wall clock, so replay cannot reproduce the same decision"),
        remediation="read engine time from context.clock, for example context.clock.now()",
    ),
    SandboxRule(
        code="SB002",
        category=ViolationCategory.WALL_CLOCK,
        module="datetime",
        attributes=frozenset(
            {"now", "today", "utcnow", "utcfromtimestamp", "fromtimestamp"}
        ),
        summary=(
            "reads the current date or time, so replay cannot reproduce the same"
            " decision"
        ),
        remediation=(
            "build timestamps explicitly, for example datetime(2024, 1, 2,"
            " tzinfo=UTC), or read context.clock"
        ),
    ),
    SandboxRule(
        code="SB003",
        category=ViolationCategory.WALL_CLOCK,
        module="os",
        attributes=frozenset({"times", "clock_gettime"}),
        summary="reads process CPU or wall time outside the injected clock",
        remediation="use context.clock for every timestamp a strategy observes",
    ),
    SandboxRule(
        code="SB100",
        category=ViolationCategory.SLEEPING,
        module="time",
        attributes=frozenset({"sleep"}),
        summary="sleeps inside the strategy and stalls the deterministic event loop",
        remediation=(
            "schedule a timestamped event through context.scheduler instead of sleeping"
        ),
    ),
    SandboxRule(
        code="SB200",
        category=ViolationCategory.SUBPROCESS,
        module="subprocess",
        attributes=None,
        summary="starts another process, which the kernel cannot order or replay",
        remediation=(
            "express side effects through context.orders or context.scheduler"
            " instead of a subprocess"
        ),
    ),
    SandboxRule(
        code="SB201",
        category=ViolationCategory.SUBPROCESS,
        module="pty",
        attributes=None,
        summary="spawns another process through a pseudo-terminal",
        remediation=(
            "express side effects through context.orders or context.scheduler"
            " instead of a subprocess"
        ),
    ),
    SandboxRule(
        code="SB202",
        category=ViolationCategory.SUBPROCESS,
        module="os",
        attributes=frozenset(
            {
                "execl",
                "execle",
                "execlp",
                "execv",
                "execve",
                "execvp",
                "execvpe",
                "fork",
                "forkpty",
                "popen",
                "posix_spawn",
                "posix_spawnp",
                "spawnl",
                "spawnle",
                "spawnlp",
                "spawnv",
                "spawnve",
                "spawnvp",
                "spawnvpe",
                "system",
            }
        ),
        summary="starts another process, which the kernel cannot order or replay",
        remediation=(
            "express side effects through context.orders or context.scheduler"
            " instead of a subprocess"
        ),
    ),
    *_network_rules(),
    SandboxRule(
        code="SB400",
        category=ViolationCategory.UNSEEDED_RANDOM,
        module="random",
        attributes=frozenset(
            {
                "SystemRandom",
                "betavariate",
                "choice",
                "choices",
                "expovariate",
                "gammavariate",
                "gauss",
                "getrandbits",
                "getstate",
                "lognormvariate",
                "normalvariate",
                "paretovariate",
                "randint",
                "random",
                "randrange",
                "sample",
                "seed",
                "setstate",
                "shuffle",
                "triangular",
                "uniform",
                "vonmisesvariate",
                "weibullvariate",
            }
        ),
        summary=("draws process-global randomness that replay cannot reproduce"),
        remediation=(
            "use the seeded generator supplied by the context, for example"
            " context.random.random()"
        ),
    ),
    SandboxRule(
        code="SB401",
        category=ViolationCategory.UNSEEDED_RANDOM,
        module="secrets",
        attributes=None,
        summary="draws cryptographic entropy that replay cannot reproduce",
        remediation=(
            "use the seeded generator supplied by the context, for example"
            " context.random.random()"
        ),
    ),
    SandboxRule(
        code="SB402",
        category=ViolationCategory.UNSEEDED_RANDOM,
        module="os",
        attributes=frozenset({"urandom", "getrandom"}),
        summary="draws operating-system entropy that replay cannot reproduce",
        remediation=(
            "use the seeded generator supplied by the context, for example"
            " context.random.random()"
        ),
    ),
    SandboxRule(
        code="SB403",
        category=ViolationCategory.UNSEEDED_RANDOM,
        module="uuid",
        attributes=frozenset({"uuid1", "uuid4"}),
        summary="derives an identifier from the clock or from entropy",
        remediation=(
            "derive identifiers deterministically from context.state or from an"
            " event identifier"
        ),
    ),
    *_async_rules(),
    *_concurrency_rules(),
)

ASYNC_SYNTAX_RULE = SandboxRule(
    code="SB700",
    category=ViolationCategory.ASYNCIO,
    module="",
    attributes=None,
    summary="uses async syntax, which cannot run inside the synchronous kernel",
    remediation=(
        "implement callbacks synchronously and schedule timestamped events"
        " through context.scheduler"
    ),
)

_WHOLE_MODULE_RULES: dict[str, SandboxRule] = {
    rule.module: rule for rule in RULES if rule.attributes is None
}

_ATTRIBUTE_RULES: dict[tuple[str, str], SandboxRule] = {
    (rule.module, attribute): rule
    for rule in RULES
    if rule.attributes is not None
    for attribute in sorted(rule.attributes)
}


def _dotted_parts(expression: ast.expr) -> list[str] | None:
    """Return the dotted name of a pure name/attribute chain.

    A chain that passes through a call result is intentionally not a dotted
    module path: ``random.Random(7).random()`` calls a method on a returned
    object rather than on the ``random`` module.
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


class _SandboxVisitor(ast.NodeVisitor):
    """Collect sandbox violations from one parsed strategy module."""

    def __init__(self, filename: str, lineno_offset: int = 0) -> None:
        self._filename = filename
        self._lineno_offset = lineno_offset
        self._violations: list[SandboxViolation] = []
        self._modules: dict[str, str] = {}
        self._reported_names: set[str] = set()
        self._reported_modules: set[str] = set()
        self._checked: set[int] = set()

    @property
    def violations(self) -> tuple[SandboxViolation, ...]:
        """Return violations ordered by source position."""
        return tuple(
            sorted(
                self._violations,
                key=lambda violation: (
                    violation.lineno,
                    violation.col_offset,
                    violation.code,
                ),
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module = alias.name
            root = module.split(".")[0]
            self._modules[alias.asname or root] = module
            rule = _WHOLE_MODULE_RULES.get(root)
            if rule is None or root in self._reported_modules:
                continue
            self._reported_modules.add(root)
            self._record(rule, node, symbol=module)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            return
        module = node.module or ""
        root = module.split(".")[0] if module else ""
        for alias in node.names:
            bound = alias.asname or alias.name
            rule = _ATTRIBUTE_RULES.get((root, alias.name)) if root else None
            if rule is None and root:
                rule = _WHOLE_MODULE_RULES.get(root)
            if rule is None:
                if module:
                    self._modules[bound] = f"{module}.{alias.name}"
                continue
            self._reported_names.add(bound)
            symbol = f"{module}.{alias.name}" if module else alias.name
            self._record(rule, node, symbol=symbol)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_reference(node.func, node)
        self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_reference(node, node)
        self.visit(node.value)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record(ASYNC_SYNTAX_RULE, node, symbol="async def")
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self._record(ASYNC_SYNTAX_RULE, node, symbol="await")
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._record(ASYNC_SYNTAX_RULE, node, symbol="async with")
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._record(ASYNC_SYNTAX_RULE, node, symbol="async for")
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        if node.is_async:
            self._record(
                ASYNC_SYNTAX_RULE, node, symbol="async for", position=node.iter
            )
        self.generic_visit(node)

    def _check_reference(self, expression: ast.expr, node: ast.AST) -> None:
        if id(expression) in self._checked:
            return
        self._checked.add(id(expression))
        parts = _dotted_parts(expression)
        if parts is None:
            return
        name, *chain = parts
        module = self._modules.get(name)
        if module is None or not chain:
            return
        root = module.split(".")[0]
        if root in self._reported_modules:
            return
        rule = _ATTRIBUTE_RULES.get((root, chain[-1]))
        if rule is None:
            return
        self._record(rule, node, symbol=".".join([module, *chain]))

    def _record(
        self,
        rule: SandboxRule,
        node: ast.AST,
        *,
        symbol: str,
        position: ast.AST | None = None,
    ) -> None:
        anchor = node if position is None else position
        lineno = int(getattr(anchor, "lineno", 0)) + self._lineno_offset
        self._violations.append(
            SandboxViolation(
                filename=self._filename,
                lineno=lineno,
                col_offset=int(getattr(anchor, "col_offset", 0)),
                code=rule.code,
                category=rule.category,
                symbol=symbol,
                message=f"{symbol}: {rule.summary}",
                remediation=rule.remediation,
            )
        )


def lint_strategy_source(
    source: str,
    *,
    filename: str = DEFAULT_FILENAME,
    lineno_offset: int = 0,
) -> LintReport:
    """Lint strategy source text and return every sandbox violation.

    ``lineno_offset`` shifts reported line numbers so a class or module excerpt
    can report positions in its original file.
    """
    if not isinstance(source, str):
        raise TypeError("source must be strategy source text")
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        raise StrategyLintError(f"{filename} is not valid Python: {error}") from error
    visitor = _SandboxVisitor(filename, lineno_offset)
    visitor.visit(tree)
    return LintReport(filename, visitor.violations)


def lint_strategy_file(path: str | Path) -> LintReport:
    """Lint one strategy file and return every sandbox violation."""
    target = Path(path)
    return lint_strategy_source(
        target.read_text(encoding="utf-8-sig"), filename=str(target)
    )


def lint_strategy_class(
    strategy_class: type,
    *,
    filename: str | None = None,
) -> LintReport:
    """Lint one strategy class and return every sandbox violation.

    The surrounding module is linted when the class has a source file, because
    imports live outside the class body; only violations whose line falls inside
    the class block are reported, so diagnostics stay scoped to the strategy.
    """
    if not isinstance(strategy_class, type):
        raise TypeError("strategy_class must be a class")
    try:
        lines, start = inspect.getsourcelines(strategy_class)
    except (OSError, TypeError) as error:
        raise StrategyLintError(
            f"cannot read source for {strategy_class.__qualname__}: {error}"
        ) from error
    resolved = (
        filename
        or inspect.getsourcefile(strategy_class)
        or f"<class {strategy_class.__qualname__}>"
    )
    source_file = Path(resolved)
    if not source_file.is_file():
        return lint_strategy_source(
            textwrap.dedent("".join(lines)),
            filename=resolved,
            lineno_offset=max(start - 1, 0),
        )
    report = lint_strategy_file(source_file)
    end = start + len(lines)
    scoped = tuple(
        violation for violation in report.violations if start <= violation.lineno < end
    )
    return LintReport(resolved, scoped)


def lint_strategy_module(module: ModuleType) -> LintReport:
    """Lint one strategy module and return every sandbox violation."""
    if not isinstance(module, ModuleType):
        raise TypeError("module must be a module object")
    module_file = getattr(module, "__file__", None)
    if isinstance(module_file, str) and Path(module_file).is_file():
        return lint_strategy_file(module_file)
    name = str(getattr(module, "__name__", DEFAULT_FILENAME))
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError) as error:
        raise StrategyLintError(
            f"cannot read source for module {name}: {error}"
        ) from error
    return lint_strategy_source(textwrap.dedent(source), filename=f"<module {name}>")


StrategyTarget = str | Path | type | ModuleType


def _looks_like_path(text: str) -> bool:
    return "\n" not in text and text.endswith(".py")


def lint_strategy(
    target: StrategyTarget,
    *,
    filename: str | None = None,
) -> LintReport:
    """Lint a strategy given as source text, a file path, a class, or a module.

    Source text is the default interpretation for a ``str`` target; a ``str``
    that ends with ``.py`` and names an existing file is read from disk so the
    same entry point works for CI paths and inline snippets.
    """
    if isinstance(target, ModuleType):
        return lint_strategy_module(target)
    if isinstance(target, type):
        return lint_strategy_class(target, filename=filename)
    if isinstance(target, (str, Path)):
        if isinstance(target, Path):
            return lint_strategy_file(target)
        if _looks_like_path(target) and Path(target).is_file():
            return lint_strategy_file(target)
        return lint_strategy_source(target, filename=filename or DEFAULT_FILENAME)
    raise TypeError(
        "target must be strategy source text, a file path, a class, or a module"
    )


def iter_strategy_files(root: str | Path) -> tuple[Path, ...]:
    """Return every Python file under ``root`` in stable lexical order."""
    base = Path(root)
    if not base.is_dir():
        raise StrategyLintError(f"{base} is not a directory")
    return tuple(
        sorted(
            path
            for path in base.rglob("*.py")
            if "__pycache__" not in path.parts and not path.name.startswith(".")
        )
    )


def lint_strategy_directory(root: str | Path) -> LintReport:
    """Lint every Python file under ``root`` and aggregate the violations."""
    violations: list[SandboxViolation] = []
    for path in iter_strategy_files(root):
        violations.extend(lint_strategy_file(path).violations)
    ordered = tuple(
        sorted(
            violations,
            key=lambda violation: (
                violation.filename,
                violation.lineno,
                violation.col_offset,
                violation.code,
            ),
        )
    )
    return LintReport(str(Path(root)), ordered)


def assert_sandbox_clean(
    target: StrategyTarget,
    *,
    filename: str | None = None,
) -> LintReport:
    """Lint ``target`` and raise :class:`SandboxViolationError` when unclean."""
    report = lint_strategy(target, filename=filename)
    report.raise_for_status()
    return report


def iter_rules(category: ViolationCategory | None = None) -> Iterator[SandboxRule]:
    """Yield sandbox rules in code order, optionally filtered by category."""
    for rule in RULES:
        if category is None or rule.category is category:
            yield rule
