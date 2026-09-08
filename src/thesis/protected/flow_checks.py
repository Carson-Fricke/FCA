"""Information-flow screening and allowlist-driven output sanitization.

Two static, deterministic checks guard the identity boundary of a protected
RCA release:

``screen_source`` screens the original strategy source before release.
Identity-revealing content - hardcoded real identifiers, author contacts,
absolute paths and filenames, endpoints, identity-bearing comments, and date
literals - fails the release closed.  The authority never rewrites original
source to hide an identity; once the source itself defeats mapping secrecy,
protected RCA is blocked.

``sanitize_output`` transforms released artifacts through an allowlist of
output surfaces: filesystem paths, tags, log lines, error messages,
timestamps, timing/latency signatures, endpoints, and contacts that are not
allowlisted are replaced with neutral tokens.  Timing is deliberately
special: even when the surface is allowlisted it is transformed to an
order-of-magnitude bucket, never left at full precision, because precise
latency signatures fingerprint identity.

``assert_no_raw_identifiers`` is the deterministic egress check for advice
returning from the named RCA session to the main Thesis agent: a raw alias
or a raw real identifier raises instead of passing through.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from thesis.protected.aliasing import IdentityMapping
from thesis.protected.contracts import ProtectedTestError

_EXCERPT_CONTEXT = 12
_VALUE_GUARD = r"[A-Za-z0-9_@/:~-]"


class FlowCheckError(ProtectedTestError):
    """An information-flow check received an unusable input."""


class IdentifierLeakError(FlowCheckError):
    """Raw identifiers reached a payload that must not carry them."""


class FlowFindingKind(StrEnum):
    """The categories of identity-bearing content the checks detect."""

    IDENTIFIER = "identifier"
    CONTACT = "contact"
    ABSOLUTE_PATH = "absolute-path"
    ENDPOINT = "endpoint"
    IDENTITY_COMMENT = "identity-comment"
    DATE_LITERAL = "date-literal"
    TIMESTAMP = "timestamp"
    TIMING = "timing"
    TAG = "tag"
    LOG_LINE = "log-line"
    ERROR_MESSAGE = "error-message"


class OutputSurface(StrEnum):
    """The allowlisted output surfaces of released artifacts."""

    PATH = "path"
    CONTACT = "contact"
    ENDPOINT = "endpoint"
    TIMESTAMP = "timestamp"
    TIMING = "timing"
    TAG = "tag"
    LOG_LINE = "log-line"
    ERROR_MESSAGE = "error-message"


@dataclass(frozen=True, slots=True)
class FlowFinding:
    """One detected identity or side-channel occurrence.

    The excerpt masks the matched content itself: findings locate and
    categorize identity-bearing content without echoing it into logs.
    """

    kind: FlowFindingKind
    line: int
    column: int
    length: int
    excerpt: str

    def describe(self) -> str:
        """Return a precise, content-free description for fail-closed errors."""
        return (
            f"{self.kind.value} at line {self.line}, column {self.column} "
            f"({self.length} characters)"
        )


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts


def _position(starts: list[int], offset: int) -> tuple[int, int]:
    line = bisect_right(starts, offset)
    return line, offset - starts[line - 1] + 1


def _masked_excerpt(text: str, start: int, end: int) -> str:
    before = text[max(0, start - _EXCERPT_CONTEXT) : start].replace("\n", " ")
    after = text[end : end + _EXCERPT_CONTEXT].replace("\n", " ")
    return f"{before}***{after}"


def _finding(
    text: str, starts: list[int], start: int, end: int, kind: FlowFindingKind
) -> FlowFinding:
    line, column = _position(starts, start)
    return FlowFinding(
        kind=kind,
        line=line,
        column=column,
        length=end - start,
        excerpt=_masked_excerpt(text, start, end),
    )


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_IDENTITY_COMMENT = re.compile(
    r"(?i:(?:#|//)[^\n]*(?:\bauthor\b|maintainer|owner|copyright|\(c\)"
    r"|written\s+by|created\s+by|contact)[^\n]*)"
)
_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]{1,160}")
_POSIX_PATH = re.compile(
    r"(?<![\w.])/(?:home|Users|root|var|tmp|opt|mnt|srv|workspace|repos)"
    r"(?:/[^\s\"'<>|]{0,160})?"
)
_URL = re.compile(r"\bhttps?://[^\s\"'<>]{1,240}")
_IP_PORT = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b")
_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def screen_source(
    text: str, *, known_identifiers: Sequence[str] = ()
) -> list[FlowFinding]:
    """Screen original strategy source for identity-revealing content.

    Detects hardcoded real identifiers (against the supplied known set),
    author contacts, absolute paths and filenames, endpoints, identity-
    bearing comments, and date literals.  Any finding blocks the release:
    the caller fails closed instead of relabeling or rewriting the source.
    """
    if not isinstance(text, str):
        raise FlowCheckError("source text must be a string")
    starts = _line_starts(text)
    spans: set[tuple[int, int, FlowFindingKind]] = set()
    for identifier in known_identifiers:
        if not isinstance(identifier, str) or not identifier:
            raise FlowCheckError("known identifiers must be non-empty strings")
        pattern = re.compile(
            f"(?<!{_VALUE_GUARD}){re.escape(identifier)}(?!{_VALUE_GUARD})"
        )
        for match in pattern.finditer(text):
            spans.add((match.start(), match.end(), FlowFindingKind.IDENTIFIER))
    for match in _EMAIL.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.CONTACT))
    for match in _IDENTITY_COMMENT.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.IDENTITY_COMMENT))
    for match in _WIN_PATH.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.ABSOLUTE_PATH))
    for match in _POSIX_PATH.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.ABSOLUTE_PATH))
    for match in _URL.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.ENDPOINT))
    for match in _IP_PORT.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.ENDPOINT))
    for match in _DATE.finditer(text):
        spans.add((match.start(), match.end(), FlowFindingKind.DATE_LITERAL))
    return [
        _finding(text, starts, start, end, kind)
        for start, end, kind in sorted(spans, key=lambda item: (item[0], item[2].value))
    ]


_TIMING = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:µs|us|ms|s)(?![\w.])")

_SANITIZER_FRAGMENTS: tuple[tuple[str, str], ...] = (
    (
        "log_line",
        r"(?P<log_line>^[^\S\n]*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        r"[^\n]*?(?:INFO|DEBUG|TRACE|WARNING|WARN|ERROR|CRITICAL|FATAL)\b[^\n]*)",
    ),
    (
        "traceback_header",
        r"(?P<traceback_header>^[^\S\n]*Traceback \(most recent call last\):[^\n]*)",
    ),
    ("win_path", f"(?P<win_path>{_WIN_PATH.pattern})"),
    ("posix_path", f"(?P<posix_path>{_POSIX_PATH.pattern})"),
    ("url", f"(?P<url>{_URL.pattern})"),
    ("ip_port", f"(?P<ip_port>{_IP_PORT.pattern})"),
    ("email", f"(?P<email>{_EMAIL.pattern})"),
    (
        "error_message",
        r"(?P<error_message>(?i:\b(?:error|exception|traceback)\s*[:=]\s*[^\n]{0,200}))",
    ),
    (
        "iso_datetime",
        r"(?P<iso_datetime>(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    ),
    ("iso_date", r"(?P<iso_date>(?<!\d)\d{4}-\d{2}-\d{2}(?!\d))"),
    (
        "timing",
        r"(?P<timing>(?<![\w.])\d+(?:\.\d+)?\s?(?:µs|us|ms|s)(?![\w.]))",
    ),
    (
        "tags_literal",
        r"(?P<tags_literal>(?i:\btags?\s*[:=]\s*\[[^\]\n]{0,200}\]))",
    ),
    ("hashtag", r"(?P<hashtag>(?<![A-Za-z0-9_])#[A-Za-z0-9_][A-Za-z0-9_-]{0,63})"),
)

_SURFACE_BY_GROUP: dict[str, tuple[OutputSurface, FlowFindingKind, str]] = {
    "log_line": (OutputSurface.LOG_LINE, FlowFindingKind.LOG_LINE, "[log]"),
    "traceback_header": (
        OutputSurface.ERROR_MESSAGE,
        FlowFindingKind.ERROR_MESSAGE,
        "[error]",
    ),
    "win_path": (OutputSurface.PATH, FlowFindingKind.ABSOLUTE_PATH, "[path]"),
    "posix_path": (OutputSurface.PATH, FlowFindingKind.ABSOLUTE_PATH, "[path]"),
    "url": (OutputSurface.ENDPOINT, FlowFindingKind.ENDPOINT, "[endpoint]"),
    "ip_port": (OutputSurface.ENDPOINT, FlowFindingKind.ENDPOINT, "[endpoint]"),
    "email": (OutputSurface.CONTACT, FlowFindingKind.CONTACT, "[contact]"),
    "error_message": (
        OutputSurface.ERROR_MESSAGE,
        FlowFindingKind.ERROR_MESSAGE,
        "[error]",
    ),
    "iso_datetime": (OutputSurface.TIMESTAMP, FlowFindingKind.TIMESTAMP, "[timestamp]"),
    "iso_date": (OutputSurface.TIMESTAMP, FlowFindingKind.TIMESTAMP, "[timestamp]"),
    "timing": (OutputSurface.TIMING, FlowFindingKind.TIMING, "[timing]"),
    "tags_literal": (OutputSurface.TAG, FlowFindingKind.TAG, "[tags]"),
    "hashtag": (OutputSurface.TAG, FlowFindingKind.TAG, "[tag]"),
}

_SANITIZER_PATTERN = re.compile(
    "|".join(fragment for _, fragment in _SANITIZER_FRAGMENTS),
    re.MULTILINE,
)


def _timing_bucket(token: str) -> str:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s?(µs|us|ms|s)", token.strip())
    if match is None:
        return "[timing]"
    value = float(match.group(1))
    unit = match.group(2)
    order = 0 if value < 1 else 1 if value < 10 else 2 if value < 100 else 3
    return f"[timing:{unit}{order}]"


def sanitize_output(
    text: str, allowlist: Iterable[OutputSurface] = ()
) -> tuple[str, tuple[FlowFinding, ...]]:
    """Remove or transform side channels that are not on the allowlist.

    Returns the sanitized text and the findings removed from it.  Allowlisted
    surfaces pass through untouched, except timing, which is always at least
    reduced to an order-of-magnitude bucket.  Deterministic for a given text
    and allowlist.
    """
    if not isinstance(text, str):
        raise FlowCheckError("output text must be a string")
    allowed: set[OutputSurface] = set()
    for surface in allowlist:
        if not isinstance(surface, OutputSurface):
            raise FlowCheckError("allowlist entries must be OutputSurface values")
        allowed.add(surface)
    starts = _line_starts(text)
    removed: list[FlowFinding] = []

    def _substitute(match: re.Match[str]) -> str:
        group = match.lastgroup
        if group is None or group not in _SURFACE_BY_GROUP:
            return match.group(0)
        surface, kind, token = _SURFACE_BY_GROUP[group]
        if surface in allowed:
            if surface is OutputSurface.TIMING:
                return _timing_bucket(match.group(0))
            return match.group(0)
        removed.append(_finding(text, starts, match.start(), match.end(), kind))
        if surface is OutputSurface.TIMING:
            return "[timing]"
        return token

    sanitized = _SANITIZER_PATTERN.sub(_substitute, text)
    return sanitized, tuple(
        sorted(
            removed,
            key=lambda item: (item.line, item.column, item.kind.value),
        )
    )


def assert_no_raw_identifiers(advice_text: str, mapping: IdentityMapping) -> None:
    """Fail closed when advice carries raw aliases or raw real identifiers.

    The egress filter for the main Thesis agent: advice may reference
    identities only through the neutral egress tokens substituted by
    :meth:`IdentityMapping.replace_aliases_for_egress`.
    """
    if not isinstance(advice_text, str):
        raise FlowCheckError("advice text must be a string")
    if not isinstance(mapping, IdentityMapping):
        raise FlowCheckError("mapping must be an IdentityMapping")
    for alias in mapping.aliases():
        pattern = re.compile(f"(?<!{_VALUE_GUARD}){re.escape(alias)}(?!{_VALUE_GUARD})")
        if pattern.search(advice_text) is not None:
            raise IdentifierLeakError(
                f"filtered advice contains raw release alias {alias!r}"
            )
    for real in mapping.real_values():
        pattern = re.compile(f"(?<!{_VALUE_GUARD}){re.escape(real)}(?!{_VALUE_GUARD})")
        if pattern.search(advice_text) is not None:
            surface = mapping.surface_for_real(real)
            label = surface.value if surface is not None else "identity"
            raise IdentifierLeakError(
                f"filtered advice contains a raw {label} identifier"
            )


__all__ = [
    "FlowCheckError",
    "FlowFinding",
    "FlowFindingKind",
    "IdentifierLeakError",
    "OutputSurface",
    "assert_no_raw_identifiers",
    "sanitize_output",
    "screen_source",
]
