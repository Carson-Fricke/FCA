"""Pure cross-language SDK source-digest contract goldens."""

import pytest

from thesis.workspaces.sdk import source_digest

SOURCE = "// café\npub const VALUE: u32 = 7;\n"
# Shared with harness strategy-jit/tests/unit/sdk_reference.rs.
IDENTITY = "1082bf8ebecd95e5b8b748ba79f12aa232bc8364fe2d4d3d50081b550104a0d4"


@pytest.mark.parametrize(
    "source",
    [SOURCE, SOURCE.replace("\n", "\r\n"), SOURCE.replace("\n", "\r\n", 1)],
)
def test_sdk_digest_ignores_only_crlf_line_endings(source: str) -> None:
    assert source_digest(source.encode()) == IDENTITY


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("7", "8"),
        SOURCE.replace(" = ", "  = "),
        SOURCE.rstrip(),
        SOURCE.replace("café", "cafe"),
        SOURCE.replace("\n", "\r"),
    ],
)
def test_sdk_digest_retains_all_other_source_differences(source: str) -> None:
    assert source_digest(source.encode()) != IDENTITY
