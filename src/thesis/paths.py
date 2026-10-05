"""The portable relative-path rule shared by authored workspace artifacts.

Strategy bundles and research models publish user-authored files keyed by
relative paths.  One rule rejects absolute paths, traversal, Windows-reserved
characters and device names, and trailing dots or spaces before any filesystem
write, so the two stores cannot drift apart.
"""

from __future__ import annotations

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class InvalidRelativePath(ValueError):
    """Raised when text is not a portable, confined relative path."""


def confined_relative_path(value: object, label: str = "path") -> str:
    """Return ``value`` normalized to POSIX separators when it is confined.

    Paths are accepted only when every forward-slash-separated component is a
    portable file name: no absolute prefix or drive, no empty, ``.`` or ``..``
    component, and no character or device name Windows rejects.
    """
    if not isinstance(value, str):
        raise InvalidRelativePath(f"{label} must be a string")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            ":" in part
            or part.casefold() in {".git", "manifest.json"}
            or part.endswith((".", " "))
            or any(ord(character) < 32 or character in '<>"|?*' for character in part)
            or part.split(".")[0].casefold() in _WINDOWS_RESERVED_NAMES
            for part in parts
        )
    ):
        raise InvalidRelativePath(f"{label} must be a confined relative path")
    return "/".join(parts)


__all__ = ["InvalidRelativePath", "confined_relative_path"]
