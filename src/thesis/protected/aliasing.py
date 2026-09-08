"""Authority-side RCA aliasing selected by evidence tier and release gates.

Aliasing and de-identification are not user-facing Thesis modes.  They are
authority-side aliasing strategies invoked when a protected result is
released to a named RCA subagent session.  The aliasing strategy and the
identity surfaces it covers are selected programmatically from the requested
RCA evidence tier and the per-strategy release-gate decisions (empirical
reidentification and diagnostic-utility thresholds plus the cross-release
disclosure ledger); there is no user switch.

This module encodes the versioned ``fca-thesis-aliasing-policy-v1``
selection policy derived from ``.github/architecture/fca-thesis.md``
("RCA Aliasing Strategies" and "RCA Subagent Evidence Protocol"):

- ``full-aliased`` (highest tier): hardened de-identification when the gate
  allows it, otherwise identity masking, over every identity surface.
- ``cutoff-constrained`` (second-highest tier, the standard practice):
  identity masking over every identity surface.
- ``summary-blinded``: identity masking over the identifier surfaces and
  timestamps only; market identity surfaces are withheld from summaries.
- ``no-release``: the tier authorizes no RCA release and fails closed.

Aliases are derived deterministically from a fresh per-release salt: stable
within one release, different across releases, and never a public hash of
the real value.  Timestamps become neutral interval counters (``t=8321``
style) that preserve minute-granularity ordering and spacing while
suppressing absolute calendar dates.

The real-to-alias mapping lives only here, in the release authority layer.
It is never serialized into a released artifact, manifest, filtered advice,
prompt, log record, or trace span.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from thesis.protected.contracts import ProtectedTestError

ALIASING_POLICY_VERSION = "fca-thesis-aliasing-policy-v1"

_DIGEST_WIDTHS = (6, 8, 12, 16, 32, 64)
_ALIAS_SALT_KEY_SIZE = 32
_VALUE_GUARD = r"[A-Za-z0-9_@/:~-]"
_ALIAS_GUARD = r"[A-Za-z0-9_@/:~=-]"
_DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}")


class AliasingError(ProtectedTestError):
    """An alias plan, gate, or identity mapping would be inconsistent."""


class ReleaseBlockedError(ProtectedTestError):
    """A protected RCA release is blocked; the release fails closed.

    The reasons are precise (surface, category, location) and never echo the
    identity-revealing content itself, so the error cannot leak the very
    identity it is blocking.
    """

    def __init__(self, reasons: Sequence[str], *, message: str | None = None) -> None:
        self.reasons = tuple(reasons)
        if not self.reasons:
            raise AliasingError("a blocked release requires at least one reason")
        super().__init__(
            message if message is not None else _blocked_message(self.reasons)
        )


def _blocked_message(reasons: tuple[str, ...]) -> str:
    joined = "; ".join(reasons)
    return f"protected RCA release blocked: {joined}"


class AliasSelectionError(ReleaseBlockedError):
    """The evidence tier and release gates authorize no aliasing strategy."""


class IdentitySurface(StrEnum):
    """One identity surface that aliasing may cover in released payloads."""

    STRATEGY_ID = "strategy-id"
    EXPERIMENT_ID = "experiment-id"
    LINEAGE_ID = "lineage-id"
    SYMBOL = "symbol"
    TIMESTAMP = "timestamp"
    VENUE = "venue"
    SECTOR = "sector"


class AliasStrategyKind(StrEnum):
    """The two authority-side aliasing strategies from the architecture."""

    IDENTITY_MASKING = "identity-masking"
    HARDENED_DEIDENTIFICATION = "hardened-deidentification"


class RcaEvidenceTier(StrEnum):
    """The RCA subagent evidence tiers of the versioned aliasing policy."""

    FULL_ALIASED = "full-aliased"
    CUTOFF_CONSTRAINED = "cutoff-constrained"
    SUMMARY_BLINDED = "summary-blinded"
    NO_RELEASE = "no-release"


@dataclass(frozen=True, slots=True)
class StrategyGateDecision:
    """The release-gate verdict for one aliasing strategy.

    ``empirical_reidentification_ok`` and ``diagnostic_utility_ok`` mirror
    the architecture's empirical attack and diagnostic-utility thresholds; a
    failing decision must carry a precise reason.
    """

    strategy: AliasStrategyKind
    empirical_reidentification_ok: bool
    diagnostic_utility_ok: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, AliasStrategyKind):
            raise AliasingError("strategy must be an AliasStrategyKind")
        if not (self.empirical_reidentification_ok and self.diagnostic_utility_ok):
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise AliasingError(
                    "a failing strategy gate decision requires a reason"
                )


@dataclass(frozen=True, slots=True)
class ReleaseGate:
    """Release-gate state feeding aliasing selection (never a user mode).

    ``cross_release_disclosure_ok`` reflects the cross-release disclosure
    ledger for overlapping windows, correlated assets, and prior recipients;
    one :class:`StrategyGateDecision` must be present per aliasing strategy.
    """

    cross_release_disclosure_ok: bool
    decisions: tuple[StrategyGateDecision, ...]

    def __post_init__(self) -> None:
        decisions = tuple(self.decisions)
        for decision in decisions:
            if not isinstance(decision, StrategyGateDecision):
                raise AliasingError(
                    "decisions must contain StrategyGateDecision values"
                )
        seen = [decision.strategy for decision in decisions]
        if len(seen) != len(set(seen)) or set(seen) != set(AliasStrategyKind):
            raise AliasingError(
                "release gate needs exactly one decision per aliasing strategy"
            )
        object.__setattr__(self, "decisions", decisions)

    def decision_for(self, strategy: AliasStrategyKind) -> StrategyGateDecision:
        """Return the gate decision for one aliasing strategy."""
        if not isinstance(strategy, AliasStrategyKind):
            raise AliasingError("strategy must be an AliasStrategyKind")
        for decision in self.decisions:
            if decision.strategy is strategy:
                return decision
        raise AliasingError(f"release gate has no decision for {strategy.value}")

    def allows(self, strategy: AliasStrategyKind) -> bool:
        """Return whether the gate lets this strategy through."""
        decision = self.decision_for(strategy)
        return (
            self.cross_release_disclosure_ok
            and decision.empirical_reidentification_ok
            and decision.diagnostic_utility_ok
        )


_ID_SURFACES = (
    IdentitySurface.STRATEGY_ID,
    IdentitySurface.EXPERIMENT_ID,
    IdentitySurface.LINEAGE_ID,
)
_MARKET_SURFACES = (
    IdentitySurface.SYMBOL,
    IdentitySurface.TIMESTAMP,
    IdentitySurface.VENUE,
    IdentitySurface.SECTOR,
)
_SUMMARY_SURFACES = _ID_SURFACES + (IdentitySurface.TIMESTAMP,)

_ALIAS_PREFIXES: Mapping[IdentitySurface, str] = {
    IdentitySurface.STRATEGY_ID: "strat",
    IdentitySurface.EXPERIMENT_ID: "exp",
    IdentitySurface.LINEAGE_ID: "lin",
    IdentitySurface.SYMBOL: "asset",
    IdentitySurface.VENUE: "venue",
    IdentitySurface.SECTOR: "sector",
}


@dataclass(frozen=True, slots=True)
class AliasPlan:
    """The aliasing selection for one release: strategy plus surfaces."""

    evidence_tier: RcaEvidenceTier
    strategy: AliasStrategyKind
    surfaces: tuple[IdentitySurface, ...]
    policy_version: str = ALIASING_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_tier, RcaEvidenceTier):
            raise AliasingError("evidence_tier must be an RcaEvidenceTier")
        if not isinstance(self.strategy, AliasStrategyKind):
            raise AliasingError("strategy must be an AliasStrategyKind")
        surfaces = tuple(self.surfaces)
        for surface in surfaces:
            if not isinstance(surface, IdentitySurface):
                raise AliasingError("surfaces must contain IdentitySurface values")
        object.__setattr__(self, "surfaces", tuple(dict.fromkeys(surfaces)))

    @property
    def fingerprint_suppression(self) -> bool:
        """Whether the plan additionally suppresses fingerprint surfaces."""
        return self.strategy is AliasStrategyKind.HARDENED_DEIDENTIFICATION

    def aliases_surface(self, surface: IdentitySurface) -> bool:
        """Return whether the plan aliases one identity surface."""
        if not isinstance(surface, IdentitySurface):
            raise AliasingError("surface must be an IdentitySurface")
        return surface in self.surfaces


def build_aliasing(
    evidence_tier: RcaEvidenceTier, release_gate: ReleaseGate
) -> AliasPlan:
    """Select the aliasing plan from the evidence tier and release gates.

    There is deliberately no user-facing mode argument: the strategy and the
    covered identity surfaces follow only from the tier and the gate state.
    Raises :class:`AliasSelectionError` (a :class:`ReleaseBlockedError`)
    when the tier authorizes nothing or every candidate strategy fails a
    threshold.
    """
    if not isinstance(evidence_tier, RcaEvidenceTier):
        raise AliasSelectionError(("evidence tier must be an RcaEvidenceTier",))
    if not isinstance(release_gate, ReleaseGate):
        raise AliasSelectionError(("release gate must be a ReleaseGate",))
    if evidence_tier is RcaEvidenceTier.NO_RELEASE:
        raise AliasSelectionError(
            ("evidence tier 'no-release' does not authorize any RCA release",)
        )
    if not release_gate.cross_release_disclosure_ok:
        raise AliasSelectionError(
            (
                "cross-release disclosure ledger reports unresolved disclosure "
                "for this release",
            )
        )
    if evidence_tier is RcaEvidenceTier.FULL_ALIASED:
        candidates = (
            AliasStrategyKind.HARDENED_DEIDENTIFICATION,
            AliasStrategyKind.IDENTITY_MASKING,
        )
        surfaces = _ID_SURFACES + _MARKET_SURFACES
    elif evidence_tier is RcaEvidenceTier.CUTOFF_CONSTRAINED:
        candidates = (AliasStrategyKind.IDENTITY_MASKING,)
        surfaces = _ID_SURFACES + _MARKET_SURFACES
    else:
        candidates = (AliasStrategyKind.IDENTITY_MASKING,)
        surfaces = _SUMMARY_SURFACES
    for strategy in candidates:
        if release_gate.allows(strategy):
            return AliasPlan(
                evidence_tier=evidence_tier, strategy=strategy, surfaces=surfaces
            )
    reasons = tuple(
        f"{strategy.value}: "
        f"{release_gate.decision_for(strategy).reason or 'rejected by the release gate'}"
        for strategy in candidates
    )
    raise AliasSelectionError(reasons)


def _parse_timestamp(value: str) -> datetime | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _timestamp_aliases(salt_key: bytes, values: Sequence[str]) -> dict[str, str]:
    """Map timestamps onto neutral, order-preserving interval counters.

    Counters preserve minute-granularity ordering and spacing while
    suppressing absolute calendar dates.  A salt-derived base offset moves
    the whole counter family per release so identical timestamp sets across
    releases cannot be linked by their counters.
    """
    if not values:
        return {}
    parsed: list[tuple[datetime, str]] = []
    parseable = True
    for value in values:
        moment = _parse_timestamp(value)
        if moment is None:
            parseable = False
            break
        parsed.append((moment, value))
    counters: list[tuple[str, int]] = []
    if parseable:
        parsed.sort(key=lambda item: (item[0], item[1]))
        base = parsed[0][0]
        last = 0
        for moment, value in parsed:
            raw = 1 + int((moment - base).total_seconds() // 60)
            counter = raw if raw > last else last + 1
            counters.append((value, counter))
            last = counter
    else:
        counters = [
            (value, index) for index, value in enumerate(sorted(values), start=1)
        ]
    offset = (
        int(
            hashlib.blake2b(
                b"thesis-alias:timestamp-base", key=salt_key, digest_size=4
            ).hexdigest(),
            16,
        )
        % 1_000_000
    )
    width = max(4, len(str(counters[-1][1] + offset)))
    return {value: f"t={counter + offset:0{width}d}" for value, counter in counters}


def _tag_aliases(
    salt_key: bytes, surface: IdentitySurface, values: Sequence[str]
) -> dict[str, str]:
    """Map identity values onto per-release opaque tokens.

    The token is a salt-keyed digest of a surface-domain-separated value:
    stable within one release, different across releases, and never a
    public hash of the real value.
    """
    if not values:
        return {}
    prefix = _ALIAS_PREFIXES[surface]
    for width in _DIGEST_WIDTHS:
        aliases: dict[str, str] = {}
        for value in values:
            digest = hashlib.blake2b(
                f"thesis-alias:{surface.value}\0{value}".encode(),
                key=salt_key,
                digest_size=32,
            ).hexdigest()
            aliases[value] = f"{prefix}_{digest[:width]}"
        if len(set(aliases.values())) == len(aliases):
            return aliases
    raise AliasingError(f"alias collision for surface {surface.value!r}")


class IdentityMapping:
    """The real-to-alias mapping for one release; authority-side only.

    Every method here stays inside the release authority layer.  Nothing in
    this object may be serialized into a payload that reaches a model, log
    record, trace span, or released artifact: manifests expose only alias
    tokens, surfaces, and ordinals.
    """

    __slots__ = (
        "_alias_ordinal",
        "_alias_surface",
        "_plan",
        "_release_id",
        "_reverse",
        "_table",
    )

    def __init__(
        self,
        *,
        release_id: str,
        plan: AliasPlan,
        table: Mapping[IdentitySurface, Mapping[str, str]],
    ) -> None:
        self._release_id = release_id
        self._plan = plan
        self._table: dict[IdentitySurface, dict[str, str]] = {
            surface: dict(values) for surface, values in table.items()
        }
        self._reverse: dict[str, str] = {}
        self._alias_surface: dict[str, str] = {}
        for surface in sorted(self._table, key=lambda item: item.value):
            for real, alias in self._table[surface].items():
                previous = self._reverse.get(alias)
                if previous is not None and previous != real:
                    raise AliasingError("alias collision across surfaces")
                self._reverse[alias] = real
                self._alias_surface[alias] = surface.value
        self._alias_ordinal: dict[str, int] = {}
        for surface_value in sorted(set(self._alias_surface.values())):
            group = sorted(
                alias
                for alias, value in self._alias_surface.items()
                if value == surface_value
            )
            for ordinal, alias in enumerate(group, start=1):
                self._alias_ordinal[alias] = ordinal

    @classmethod
    def build(
        cls,
        *,
        release_id: str,
        salt: str,
        plan: AliasPlan,
        values: Mapping[IdentitySurface, Sequence[str]],
    ) -> IdentityMapping:
        """Build the mapping deterministically from a per-release salt."""
        if not isinstance(release_id, str) or not release_id.strip():
            raise AliasingError("release_id must be a non-empty string")
        if not isinstance(salt, str) or not salt:
            raise AliasingError("release salt must be a non-empty string")
        if not isinstance(plan, AliasPlan):
            raise AliasingError("plan must be an AliasPlan")
        if not isinstance(values, Mapping):
            raise AliasingError("values must map IdentitySurface to sequences")
        for surface, real_values in values.items():
            if not isinstance(surface, IdentitySurface):
                raise AliasingError("values must be keyed by IdentitySurface")
            if surface not in plan.surfaces:
                raise AliasingError(
                    f"surface {surface.value!r} is not aliased by the release plan"
                )
            if isinstance(real_values, (str, bytes)) or not isinstance(
                real_values, Sequence
            ):
                raise AliasingError(
                    f"surface {surface.value!r} values must be a sequence of strings"
                )
        salt_key = hashlib.blake2b(
            salt.encode("utf-8"), digest_size=_ALIAS_SALT_KEY_SIZE
        ).digest()
        seen: dict[str, IdentitySurface] = {}
        table: dict[IdentitySurface, dict[str, str]] = {}
        for surface in plan.surfaces:
            cleaned: list[str] = []
            for real in values.get(surface) or ():
                if (
                    not isinstance(real, str)
                    or not real
                    or any(ord(character) < 32 for character in real)
                ):
                    raise AliasingError(
                        f"surface {surface.value!r} values must be non-empty "
                        "strings without control characters"
                    )
                previous = seen.get(real)
                if previous is not None and previous is not surface:
                    raise AliasingError(
                        "identity value registered on two surfaces: "
                        f"{previous.value!r} and {surface.value!r}"
                    )
                seen[real] = surface
                if real not in cleaned:
                    cleaned.append(real)
            if surface is IdentitySurface.TIMESTAMP:
                table[surface] = _timestamp_aliases(salt_key, cleaned)
            else:
                table[surface] = _tag_aliases(salt_key, surface, cleaned)
        return cls(release_id=release_id, plan=plan, table=table)

    @property
    def release_id(self) -> str:
        """The release this mapping belongs to."""
        return self._release_id

    @property
    def plan(self) -> AliasPlan:
        """The aliasing plan this mapping was built from."""
        return self._plan

    def alias_for(self, surface: IdentitySurface, value: str) -> str:
        """Return the release-stable alias for one real identity value."""
        if not isinstance(surface, IdentitySurface) or surface not in self._table:
            raise AliasingError(f"surface {surface!r} is not aliased by this release")
        table = self._table[surface]
        if not isinstance(value, str) or value not in table:
            raise AliasingError(
                f"no alias is registered for this value on surface {surface.value!r}"
            )
        return table[value]

    def surface_for_real(self, value: str) -> IdentitySurface | None:
        """Return the surface a real identity value is registered under."""
        for surface in sorted(self._table, key=lambda item: item.value):
            if value in self._table[surface]:
                return surface
        return None

    def aliases(self) -> tuple[str, ...]:
        """Return every alias token in this mapping (never the reals)."""
        return tuple(sorted(self._alias_surface))

    def real_values(self) -> tuple[str, ...]:
        """Return every real identity value; authority-side leak checks only."""
        reals: set[str] = set()
        for table in self._table.values():
            reals.update(table)
        return tuple(sorted(reals))

    def alias_manifest(self) -> tuple[dict[str, object], ...]:
        """Return the alias-only manifest entries safe for release payloads."""
        entries: list[dict[str, object]] = []
        for alias in sorted(
            self._alias_surface,
            key=lambda item: (self._alias_surface[item], self._alias_ordinal[item]),
        ):
            entries.append(
                {
                    "alias": alias,
                    "ordinal": self._alias_ordinal[alias],
                    "surface": self._alias_surface[alias],
                }
            )
        return tuple(entries)

    def apply_to_text(self, text: str) -> str:
        """Replace every known real identity value in ``text`` with its alias.

        Substitution is a single deterministic pass; longer values win over
        their shorter prefixes so a datetime is replaced before its date.
        """
        if not isinstance(text, str):
            raise AliasingError("text must be a string")
        entries: list[tuple[str, str]] = []
        for surface in sorted(self._table, key=lambda item: item.value):
            for real in sorted(self._table[surface]):
                entries.append((real, self._table[surface][real]))
        if not entries:
            return text
        entries.sort(key=lambda item: (-len(item[0]), item[0]))
        alternatives: list[str] = []
        for real, _ in entries:
            escaped = re.escape(real)
            if _DATEISH.fullmatch(real):
                alternatives.append(f"(?<!\\d){escaped}(?!\\d)")
            else:
                alternatives.append(f"(?<!{_VALUE_GUARD}){escaped}(?!{_VALUE_GUARD})")
        pattern = re.compile("|".join(alternatives))
        lookup = dict(entries)
        return pattern.sub(lambda match: lookup[match.group(0)], text)

    def replace_aliases_for_egress(self, text: str) -> str:
        """Replace raw alias tokens with neutral per-surface ordinals.

        The deterministic egress filter for advice returning to the main
        agent: raw aliases become ``[surface:ordinal]`` tokens that keep the
        diagnostic reference structure without carrying release aliases.
        """
        if not isinstance(text, str):
            raise AliasingError("text must be a string")
        aliases = sorted(self._alias_surface, key=lambda item: (-len(item), item))
        if not aliases:
            return text
        alternatives = [
            f"(?<!{_ALIAS_GUARD}){re.escape(alias)}(?!{_ALIAS_GUARD})"
            for alias in aliases
        ]
        pattern = re.compile("|".join(alternatives))

        def _substitute(match: re.Match[str]) -> str:
            alias = match.group(0)
            return f"[{self._alias_surface[alias]}:{self._alias_ordinal[alias]}]"

        return pattern.sub(_substitute, text)


__all__ = [
    "ALIASING_POLICY_VERSION",
    "AliasPlan",
    "AliasSelectionError",
    "AliasStrategyKind",
    "AliasingError",
    "IdentityMapping",
    "IdentitySurface",
    "ReleaseBlockedError",
    "ReleaseGate",
    "RcaEvidenceTier",
    "StrategyGateDecision",
    "build_aliasing",
]
