"""Consumer-owned capabilities for program SDK policy and reference transport.

Program lifecycle operations need identity agreement and authoring guidance;
the SDK owner needs only metadata and source reads. Neither port grants job
execution, repository access or workflow control.
"""

from collections.abc import Mapping
from typing import Protocol

from thesis.workspaces.program_contracts import ProgramHarnessResult, ProgramKind
from thesis_runtime.tools.reading import DEFAULT_READ_TOKENS


class SdkReferenceSource(Protocol):
    """Read the serving SDK through the existing authenticated client.

    Transport failures preserve their existing error; invalid metadata and SDK
    drift are rejected by the reference owner before guidance or execution.
    """

    def current_server_info(self) -> Mapping[str, object]: ...

    def read_sdk_reference(self, **arguments: object) -> ProgramHarnessResult: ...


class AuthoringSdk(Protocol):
    """Supply exact SDK identity and guidance without program lifecycle access.

    Invalid or unavailable references raise ProgramArtifactError. Matching
    rechecks current local/serving sources; it never compiles or repairs them.
    """

    def local_identity(self) -> str: ...

    def matched_metadata(self) -> dict[str, object]: ...

    def read_reference(
        self,
        topic: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        token_offset: int = 0,
        max_tokens: int = DEFAULT_READ_TOKENS,
    ) -> dict[str, object]: ...

    def template_source(
        self, kind: ProgramKind, configuration: object, *, template: str = "basic"
    ) -> str: ...
