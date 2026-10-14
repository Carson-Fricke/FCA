"""Build-time smoke for the installed wheel, outside repository discovery."""

from pathlib import Path
from tempfile import TemporaryDirectory

from thesis.persistence.submissions import SqliteSubmissionStore
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.programs import WorkspacePrograms

with TemporaryDirectory() as directory:
    programs = WorkspacePrograms(
        LocalWorkspaceProvider(Path(directory)),
        submissions=SqliteSubmissionStore(Path(":memory:")),
    )
    for topic in ("study", "consumer", "research_views"):
        reference = programs.read_sdk_reference(topic)
        assert reference["sdkAbiVersion"] > 0
        assert len(reference["sdkIdentity"]) == 64
        assert reference["source"]
print("Installed SDK discovery, metadata, source references, and templates passed")
