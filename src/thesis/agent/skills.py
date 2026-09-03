"""Versioned, content-addressed Thesis skills."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SkillError(ValueError):
    """Raised when a Thesis skill or skill bundle is invalid."""


def content_hash(value: bytes | str | object) -> str:
    """Return a deterministic SHA-256 digest for content or JSON values."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_name(value: str, field: str) -> None:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise SkillError(f"{field} must be a portable identifier")


def _validate_version(value: str, field: str = "version") -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise SkillError(f"{field} must be a semantic version")


@dataclass(frozen=True, slots=True)
class SkillRef:
    """A skill reference with an optional version pin."""

    skill_id: str
    version: str | None = None

    def __post_init__(self) -> None:
        _validate_name(self.skill_id, "skill_id")
        if self.version is not None:
            _validate_version(self.version)

    @classmethod
    def parse(cls, value: str) -> SkillRef:
        """Parse ``skill`` or the explicit ``skill@version`` notation."""
        if not isinstance(value, str) or not value:
            raise SkillError("skill reference must be non-empty")
        skill_id, separator, version = value.partition("@")
        if separator == "":
            return cls(skill_id)
        if not version:
            raise SkillError("skill reference version must be non-empty")
        return cls(skill_id, version)

    @property
    def key(self) -> str:
        """Return the stable reference key used in policy records."""
        return (
            self.skill_id if self.version is None else f"{self.skill_id}@{self.version}"
        )


@dataclass(frozen=True, slots=True)
class ThesisSkill:
    """One immutable instruction skill pinned by identity and version."""

    skill_id: str
    version: str
    instructions: str
    allowed_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_name(self.skill_id, "skill_id")
        _validate_version(self.version)
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise SkillError("skill instructions must be non-empty")
        if len(self.instructions) > 64_000:
            raise SkillError("skill instructions are too large")
        tools = tuple(self.allowed_tools)
        if any(not isinstance(tool, str) or not tool.strip() for tool in tools):
            raise SkillError("skill allowed_tools must contain non-empty names")
        if len(tools) != len(set(tools)):
            raise SkillError("skill allowed_tools must be unique")
        object.__setattr__(self, "allowed_tools", tuple(sorted(tools)))

    @property
    def content_hash(self) -> str:
        """Return the digest of the complete versioned skill definition."""
        return content_hash(
            {
                "allowedTools": self.allowed_tools,
                "instructions": self.instructions,
                "skillId": self.skill_id,
                "version": self.version,
            }
        )

    @property
    def reference(self) -> SkillRef:
        """Return the explicit immutable reference for this skill."""
        return SkillRef(self.skill_id, self.version)

    def as_dict(self) -> dict[str, object]:
        """Return the non-secret, canonical skill metadata."""
        return {
            "allowedTools": list(self.allowed_tools),
            "contentHash": self.content_hash,
            "skillId": self.skill_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """The exact versioned skills selected for one model role."""

    skills: tuple[ThesisSkill, ...]

    def __post_init__(self) -> None:
        skills = tuple(self.skills)
        keys = tuple(skill.reference.key for skill in skills)
        if len(keys) != len(set(keys)):
            raise SkillError("skill bundle cannot contain duplicate skill versions")
        object.__setattr__(
            self, "skills", tuple(sorted(skills, key=lambda s: s.reference.key))
        )

    @property
    def hashes(self) -> dict[str, str]:
        """Return content hashes keyed by the pinned skill reference."""
        return {skill.reference.key: skill.content_hash for skill in self.skills}

    @property
    def versions(self) -> tuple[str, ...]:
        """Return selected skill versions in stable order."""
        return tuple(f"{skill.skill_id}@{skill.version}" for skill in self.skills)

    @property
    def bundle_hash(self) -> str:
        """Return the digest of skill identities and their content hashes."""
        return content_hash(
            {
                "skills": [
                    {
                        "contentHash": skill.content_hash,
                        "skillId": skill.skill_id,
                        "version": skill.version,
                    }
                    for skill in self.skills
                ]
            }
        )


class SkillRegistry:
    """In-memory registry that resolves only known, immutable skill versions."""

    def __init__(self, skills: Iterable[ThesisSkill] = ()) -> None:
        self._skills: dict[tuple[str, str], ThesisSkill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: ThesisSkill) -> None:
        """Add a skill version, rejecting conflicting replacement."""
        key = (skill.skill_id, skill.version)
        existing = self._skills.get(key)
        if existing is not None and existing != skill:
            raise SkillError(f"skill version already registered: {skill.reference.key}")
        self._skills[key] = skill

    def resolve(self, reference: SkillRef | str) -> ThesisSkill:
        """Resolve a pinned reference or the highest registered version."""
        ref = SkillRef.parse(reference) if isinstance(reference, str) else reference
        if ref.version is not None:
            try:
                return self._skills[(ref.skill_id, ref.version)]
            except KeyError as error:
                raise SkillError(f"unknown skill: {ref.key}") from error

        candidates = [
            skill
            for (skill_id, _), skill in self._skills.items()
            if skill_id == ref.skill_id
        ]
        if not candidates:
            raise SkillError(f"unknown skill: {ref.skill_id}")
        return max(candidates, key=lambda skill: _version_key(skill.version))

    def bundle(self, references: Iterable[SkillRef | str]) -> SkillBundle:
        """Resolve references into one deterministic skill bundle."""
        return SkillBundle(tuple(self.resolve(reference) for reference in references))

    def __iter__(self) -> Iterator[ThesisSkill]:
        return iter(
            sorted(self._skills.values(), key=lambda skill: skill.reference.key)
        )

    def __len__(self) -> int:
        return len(self._skills)


def _version_key(version: str) -> tuple[int, int, int, int, str]:
    match = re.match(
        r"^(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?$", version
    )
    if match is None:
        raise SkillError("skill version must be a semantic version")
    prerelease = match.group(4)
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if prerelease is None else 0,
        "" if prerelease is None else prerelease,
    )


__all__ = [
    "SkillBundle",
    "SkillError",
    "SkillRef",
    "SkillRegistry",
    "ThesisSkill",
    "content_hash",
]
