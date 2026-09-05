"""Sweep specifications: deduplicated parameter spaces with canonical identity.

A sweep spec pairs one base :class:`~harness.configuration.models.BacktestConfig`
with an explicit parameter space over one configured strategy instance. The
space is either a full Cartesian grid or a seeded-random sample drawn from the
same candidate values with a recorded seed, so both modes are reproducible
without wall-clock or unseeded randomness.

Sweep identity is the domain-separated SHA-256 hash of the canonical spec
document, reusing the canonical JSON conventions from
:mod:`harness.configuration.hashing`. Expansion resolves the space into
derived configurations, deduplicates them by the canonical configuration
hash computed under the base run ID, and renames each launched
configuration's ``run_id`` from that digest so identical effective
configurations collapse into one launched run both within a sweep and
across sweeps sharing the same base ``run_id``, and results catalog under
exactly the run ID the runner tracks.

Derived configurations only override one instance's ``parameters`` mapping,
so every cross-field configuration invariant of the base document is
preserved and ``model_copy`` cannot invalidate anything the spec did not
already validate at construction time.
"""

from __future__ import annotations

import hashlib
import itertools
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from harness.configuration.hashing import (
    canonical_configuration_bytes,
    canonical_configuration_json,
    configuration_hash,
)
from harness.configuration.models import BacktestConfig, ConfigurationValue

_SWEEP_DOMAIN: Final = b"fca:harness-sweeps:spec:v1\0"
_SWEEP_VERSION: Final = "harness.sweeps.spec.v1"
_IDENTIFIER_MAX: Final = 128
_RUN_ID_DIGEST: Final = 12


class SweepMode(StrEnum):
    """Parameter space sampling mode for one sweep."""

    GRID = "grid"
    RANDOM = "random"


def _space_values(
    values: Sequence[ConfigurationValue],
    name: str,
) -> tuple[ConfigurationValue, ...]:
    if not values:
        raise ValueError(f"sweep parameter {name!r} must list at least one value")
    for value in values:
        # Raises for floats and other values the canonical conventions reject,
        # which keeps every level label and sweep hash stable cross-machine.
        canonical_configuration_json(value)
    return tuple(values)


def _sweep_space(
    space: Mapping[str, Sequence[ConfigurationValue]],
) -> Mapping[str, tuple[ConfigurationValue, ...]]:
    if not space:
        raise ValueError("a sweep must declare at least one parameter")
    canonical: dict[str, tuple[ConfigurationValue, ...]] = {}
    for name, values in space.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("sweep parameter names must be non-empty strings")
        canonical[name] = _space_values(values, name)
    return MappingProxyType({name: canonical[name] for name in sorted(canonical)})


def _sweep_count(
    mode: SweepMode, space: Mapping[str, tuple[ConfigurationValue, ...]], count: int
) -> int:
    if mode is SweepMode.GRID:
        product = 1
        for values in space.values():
            product *= len(values)
        return product
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("random sweeps must request at least one draw")
    return count


@dataclass(frozen=True, slots=True)
class SweepPoint:
    """One deduplicated sweep configuration and its derived identities.

    ``configuration_digest`` is the canonical configuration hash of the
    derived configuration under the base run ID — the sweep's experiment
    identity used for deduplication. The launched ``configuration`` carries
    ``run_id`` renamed to the digest-derived ``run_id`` so results catalog
    under the same identity the runner tracks.
    """

    index: int
    parameters: Mapping[str, ConfigurationValue]
    configuration: BacktestConfig
    configuration_digest: str
    run_id: str


@dataclass(frozen=True, slots=True)
class SweepSpec:
    """Reproducible parameter sweep over one strategy instance.

    ``space`` maps parameter names to candidate values; grid mode expands the
    Cartesian product and random mode draws ``count`` seeded combinations.
    ``seed`` is recorded in the spec for random mode and must stay ``None``
    for grid mode. ``count`` is derived for grid mode as the product of the
    level counts and validated for random mode as the requested draw count.
    """

    base: BacktestConfig
    instance_id: str
    space: Mapping[str, tuple[ConfigurationValue, ...]]
    mode: SweepMode
    count: int
    seed: int | None

    @classmethod
    def grid(
        cls,
        base: BacktestConfig,
        instance_id: str,
        space: Mapping[str, Sequence[ConfigurationValue]],
    ) -> SweepSpec:
        """Build one full Cartesian-product sweep."""
        return cls(
            base=base,
            instance_id=instance_id,
            space=_sweep_space(space),
            mode=SweepMode.GRID,
            count=0,
            seed=None,
        )

    @classmethod
    def random(
        cls,
        base: BacktestConfig,
        instance_id: str,
        space: Mapping[str, Sequence[ConfigurationValue]],
        *,
        count: int,
        seed: int,
    ) -> SweepSpec:
        """Build one seeded-random sweep over the candidate values."""
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("random sweeps require a non-negative integer seed")
        return cls(
            base=base,
            instance_id=instance_id,
            space=_sweep_space(space),
            mode=SweepMode.RANDOM,
            count=count,
            seed=seed,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.base, BacktestConfig):
            raise TypeError("base must be a BacktestConfig")
        if not any(
            instance.instance_id == self.instance_id
            for instance in self.base.strategy_instances
        ):
            raise ValueError(
                f"instance_id {self.instance_id!r} is not a configured strategy instance"
            )
        mode = SweepMode(self.mode)
        object.__setattr__(self, "mode", mode)
        if mode is SweepMode.GRID and self.seed is not None:
            raise ValueError("grid sweeps must not carry a seed")
        if mode is SweepMode.RANDOM and self.seed is None:
            raise ValueError("random sweeps require a recorded seed")
        object.__setattr__(self, "count", _sweep_count(mode, self.space, self.count))

    @property
    def payload(self) -> Mapping[str, object]:
        """Return the canonical spec document backing the sweep identity."""
        return {
            "version": _SWEEP_VERSION,
            "base": self.base,
            "instance_id": self.instance_id,
            "mode": self.mode.value,
            "space": {name: list(values) for name, values in self.space.items()},
            "count": self.count,
            "seed": self.seed,
        }

    @property
    def sweep_id(self) -> str:
        """Return the domain-separated canonical hash of the whole spec."""
        return hashlib.sha256(
            _SWEEP_DOMAIN + canonical_configuration_bytes(self.payload)
        ).hexdigest()

    def _derived(self, parameters: Mapping[str, ConfigurationValue]) -> BacktestConfig:
        instance = next(
            candidate
            for candidate in self.base.strategy_instances
            if candidate.instance_id == self.instance_id
        )
        merged = {**instance.parameters, **dict(parameters)}
        updated = instance.model_copy(update={"parameters": merged})
        instances = tuple(
            updated if candidate.instance_id == self.instance_id else candidate
            for candidate in self.base.strategy_instances
        )
        return self.base.model_copy(update={"strategy_instances": instances})

    def _combinations(self) -> Iterator[tuple[ConfigurationValue, ...]]:
        names = tuple(sorted(self.space))
        if self.mode is SweepMode.GRID:
            return itertools.product(*(self.space[name] for name in names))
        seed = self.seed
        if seed is None:
            raise ValueError("random sweeps require a recorded seed")
        rng = random.Random(seed)
        return (
            tuple(rng.choice(self.space[name]) for name in names)
            for _ in range(self.count)
        )

    def expand(self) -> tuple[SweepPoint, ...]:
        """Resolve the space into deduplicated points in first-occurrence order.

        Grid mode yields the Cartesian product over parameter names in sorted
        order; random mode yields ``count`` seeded draws. Points whose derived
        configuration hash repeats an earlier point are skipped, so identical
        effective configurations collapse and the surviving points keep
        dense ``index`` values.
        """
        names = tuple(sorted(self.space))
        points: list[SweepPoint] = []
        seen: set[str] = set()
        for combo in self._combinations():
            parameters = MappingProxyType(dict(zip(names, combo, strict=True)))
            derived = self._derived(parameters)
            digest = configuration_hash(derived)
            if digest in seen:
                continue
            seen.add(digest)
            run_id = _run_id(self.base.run_id, digest)
            points.append(
                SweepPoint(
                    index=len(points),
                    parameters=parameters,
                    configuration=derived.model_copy(update={"run_id": run_id}),
                    configuration_digest=digest,
                    run_id=run_id,
                )
            )
        return tuple(points)


def _run_id(base_run_id: str, digest: str) -> str:
    """Derive one run identity from the base run ID and config digest."""
    stem = base_run_id[: _IDENTIFIER_MAX - _RUN_ID_DIGEST - 1]
    return f"{stem}-{digest[:_RUN_ID_DIGEST]}"


__all__ = [
    "SweepMode",
    "SweepPoint",
    "SweepSpec",
]
