"""Explicit window validation and canonical mandate identity (pure schema/math)."""

from copy import deepcopy
from typing import cast

import pytest

from thesis.experiments.mandate import (
    MandateError,
    ResearchMandate,
    derive_mandate,
    rfc3339_ns,
)

DECLARATION = {
    "createdAt": "2026-10-12T23:00:00Z",
    "development": {"start": "2026-01-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
    "forward": {"start": "2026-08-01T00:00:00Z", "end": "2026-10-12T23:00:00Z"},
}


def test_explicit_dates_round_trip_without_subtracting_signal_horizons():
    mandate = derive_mandate("workspace-one", "execution-one", DECLARATION)
    assert mandate.development.end_ns == rfc3339_ns("2026-08-01T00:00:00Z")
    assert mandate.development.purged_ns == mandate.forward.embargoed_ns == 0
    assert ResearchMandate.from_dict(mandate.as_dict()) == mandate
    assert (
        ResearchMandate.from_dict(mandate.as_dict()).content_sha256
        == mandate.content_sha256
    )
    assert not mandate.default_window.permits(
        mandate.development.start_ns, mandate.forward.end_ns
    )
    assert mandate.forward_window.override


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("development", "start", "2026-08-01T00:00:00Z"),
        ("development", "end", "2026-08-02T00:00:00Z"),
        ("forward", "end", "2027-01-01T00:00:00Z"),
        ("forward", "end", "2026-08-01T00:00:00Z"),
        ("forward", "start", "2026-08-01T00:00:00"),
        ("forward", "start", "invalid"),
        ("forward", "start", None),
    ],
)
def test_invalid_window_declarations_are_rejected(section, key, value):
    declaration = deepcopy(DECLARATION)
    cast(dict[str, object], declaration[section])[key] = value
    with pytest.raises(MandateError):
        derive_mandate("workspace-one", "execution-one", declaration)


def test_outdated_mandate_is_rejected():
    with pytest.raises(MandateError, match="outdated"):
        ResearchMandate.from_dict({"schema": "fca-thesis-research-mandate-v2"})
