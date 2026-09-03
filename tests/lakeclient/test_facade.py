import lakeclient


def test_package_facade_is_deliberate() -> None:
    assert lakeclient.__all__ == [
        "AssetClass",
        "CoverageEntry",
        "CoverageResponse",
        "CoverageState",
        "DataType",
        "DatasetRequest",
        "EnsureAccepted",
        "FileQueryFile",
        "FileQueryResponse",
        "FileQueryResults",
        "FileQueryState",
        "JobPollingTimeout",
        "JobState",
        "JobStatus",
        "LakeClient",
        "LakeClientError",
        "LakeProtocolError",
        "nbbo_spread",
        "read_files",
    ]
