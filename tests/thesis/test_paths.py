"""The portable relative-path rule rejects names a Windows filesystem cannot hold."""

import pytest

from thesis.paths import InvalidRelativePath, confined_relative_path


@pytest.mark.parametrize(
    "value",
    [
        "fit.data_ok < 1.0",
        "../escape.rs",
        "nested/../escape.rs",
        "/absolute.rs",
        "C:/drive.rs",
        "con.rs",
        "aux",
        ".git/config",
        "trailing.",
        "trailing ",
        "",
        "control\tcharacter.rs",
    ],
)
def test_unsafe_paths_are_rejected(value: str) -> None:
    with pytest.raises(InvalidRelativePath, match="confined relative path"):
        confined_relative_path(value, "test path")


def test_non_string_path_is_rejected() -> None:
    with pytest.raises(InvalidRelativePath, match="must be a string"):
        confined_relative_path(None, "test path")


def test_portable_paths_are_normalized() -> None:
    assert confined_relative_path("tests/model.rs") == "tests/model.rs"
    assert confined_relative_path("tests\\model.rs") == "tests/model.rs"
