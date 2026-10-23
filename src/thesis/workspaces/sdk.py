"""Canonical SDK source identities, independent of checkout line endings.

Only CRLF becomes LF. Paths and every other source byte remain significant;
program content digests and host-specific compiler provenance belong elsewhere.
"""

import hashlib
from pathlib import Path


def source_digest(source: bytes) -> str:
    """Hash SDK text with CRLF normalized; preserve all other bytes."""
    return hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest()


def source_hashes(sdk_root: Path) -> dict[str, str]:
    """Read the complete four-crate SDK inventory; missing sources fail closed."""
    hashes: dict[str, str] = {}
    for name, crate in (
        ("strategy-sdk", sdk_root),
        ("contracts", sdk_root.parent / "contracts"),
        ("num", sdk_root.parent / "num"),
        ("lake-contracts", sdk_root.parent.parent / "lake" / "contracts"),
    ):
        files = sorted((crate / "src").rglob("*.rs"))
        if not files:
            raise ValueError(f"SDK reference sources missing for {name}")
        for path in [crate / "Cargo.toml", *files]:
            hashes[f"{name}/{path.relative_to(crate).as_posix()}"] = source_digest(
                path.read_bytes()
            )
    return hashes
