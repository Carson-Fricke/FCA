"""Own SDK identity fencing, source reads and matched template rendering.

This capability consumes only the SDK reference/metadata port and packaged
sources. It never opens workspaces, commits programs, validates compilations,
dispatches jobs or waits for them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from thesis.integrations.mcp import HarnessMcpError
from thesis.workspaces.program_contracts import (
    ProgramArtifactError,
    ProgramKind,
    canonical_bytes,
    check_configuration,
)
from thesis.workspaces.sdk import identity
from thesis.workspaces.sdk.identity import sdk_metadata, source_hashes
from thesis.workspaces.sdk.ports import SdkReferenceSource
from thesis.workspaces.sdk.reading import source_page
from thesis.workspaces.sdk.templates import PROGRAM_TEMPLATES
from thesis_runtime.tools.reading import (
    DEFAULT_READ_TOKENS,
    MAX_READ_TOKENS,
    TOKENIZER,
    count_tokens,
)


def reference_topics(root: Path) -> dict[str, str]:
    """Use SDK-owned aliases plus the complete, discovered source inventory."""
    try:
        aliases = json.loads(
            (root / "reference-topics.json").read_text(encoding="utf-8")
        )
        sources = source_hashes(root)
    except (OSError, ValueError) as error:
        raise ProgramArtifactError(
            "SDK reference sources are unavailable; align the SDK deployment."
        ) from error
    if not isinstance(aliases, dict) or any(
        not isinstance(name, str) or not isinstance(path, str) or path not in sources
        for name, path in aliases.items()
    ):
        raise ProgramArtifactError(
            "SDK reference alias manifest does not match the source inventory."
        )
    return {**aliases, **{name: name for name in sources if name.endswith(".rs")}}


class SdkReferences:
    """One owner for local/serving SDK agreement and agent-visible references."""

    def __init__(
        self, source: SdkReferenceSource | None = None, *, root: Path | None = None
    ) -> None:
        self.source = source
        self.root = root if root is not None else identity.PACKAGED_SDK_ROOT

    def serving_metadata(self) -> dict[str, object]:
        """Read fresh SDK/compiler metadata through the SDK source port."""
        if self.source is None:
            raise ProgramArtifactError("harness is not configured")
        try:
            read_info = getattr(self.source, "current_server_info", None)
            metadata = read_info() if callable(read_info) else {}
            if isinstance(metadata, Mapping):
                metadata = metadata.get("serverInfo", metadata)
            if not isinstance(metadata, Mapping) or "sdkIdentity" not in metadata:
                result = self.source.read_sdk_reference()
                if result.is_error:
                    raise ProgramArtifactError("serving SDK metadata is unavailable")
                metadata = result.structured
        except (HarnessMcpError, OSError, AttributeError) as error:
            raise ProgramArtifactError("serving SDK metadata is unavailable") from error
        if (
            not isinstance(metadata, Mapping)
            or not isinstance(metadata.get("sdkIdentity"), str)
            or re.fullmatch(r"[0-9a-f]{64}", cast(str, metadata["sdkIdentity"])) is None
            or not isinstance(metadata.get("sdkVersion"), str)
            or type(metadata.get("sdkAbiVersion")) is not int
        ):
            raise ProgramArtifactError("serving SDK metadata is invalid")
        result = {
            key: metadata[key]
            for key in (
                "sdkIdentity",
                "sdkVersion",
                "sdkAbiVersion",
                "validationIdentity",
            )
            if key in metadata
        }
        compiler = result.get("validationIdentity")
        if not isinstance(compiler, str) or not compiler:
            result["validationIdentity"] = None
        return result

    def matched_metadata(self) -> dict[str, object]:
        """Fence local guidance and execution against the actual serving SDK."""
        identity = self.local_identity()
        version, abi = sdk_metadata(self.root)
        if self.source is None:
            return {
                "sdkIdentity": identity,
                "sdkVersion": version,
                "sdkAbiVersion": abi,
            }
        serving = self.serving_metadata()
        if (
            serving["sdkIdentity"],
            serving["sdkVersion"],
            serving["sdkAbiVersion"],
        ) != (identity, version, abi):
            detail = (
                f"local {identity[:12]}, serving {str(serving['sdkIdentity'])[:12]}"
            )
            try:
                reference = self.source.read_sdk_reference()
                hashes = reference.structured.get("sourceHashes")
                if (
                    not reference.is_error
                    and reference.structured.get("sdkIdentity")
                    == serving["sdkIdentity"]
                    and isinstance(hashes, Mapping)
                    and all(isinstance(name, str) for name in hashes)
                ):
                    local_hashes = source_hashes(self.root)
                    changed = sorted(
                        name
                        for name in set(local_hashes) | set(hashes)
                        if local_hashes.get(name) != hashes.get(name)
                    )
                    if changed:
                        names = ", ".join(name[:200] for name in changed[:5])
                        suffix = (
                            f" (+{len(changed) - 5} more)" if len(changed) > 5 else ""
                        )
                        detail += f"; differing sources: {names}{suffix}"
            except (HarnessMcpError, OSError, ValueError):
                # Diagnostics must not hide the original mismatch or permit execution.
                pass
            raise ProgramArtifactError(
                f"local and serving SDK identities do not match ({detail}). "
                "SDK line endings are normalized; align the local SDK sources and "
                "serving harness deployment before retrying."
            )
        return serving

    def read_reference(
        self,
        topic: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        token_offset: int = 0,
        max_tokens: int = DEFAULT_READ_TOKENS,
    ) -> dict[str, object]:
        topics = reference_topics(self.root)
        if topic == "index":
            return {**self.matched_metadata(), "topics": topics}
        if topic in PROGRAM_TEMPLATES:
            metadata = self.matched_metadata()
            text = PROGRAM_TEMPLATES[topic]
        elif topic in topics:
            if self.source is not None:
                metadata = self.serving_metadata()
                # Validate bounds locally, but preserve the serving source range
                # rather than substituting packaged reference text.
                if (
                    type(start_line) is not int
                    or start_line < 1
                    or type(max_tokens) is not int
                    or not 32 <= max_tokens <= MAX_READ_TOKENS
                    or type(token_offset) is not int
                    or token_offset < 0
                ):
                    raise ProgramArtifactError(
                        "SDK start_line must be positive, token_offset non-negative, and max_tokens between 32 and 65536."
                    )
                if end_line is not None and (
                    type(end_line) is not int or end_line < start_line
                ):
                    raise ProgramArtifactError(
                        "SDK end_line must be an integer at or after start_line; use nextArguments for continuation."
                    )
                arguments: dict[str, object] = {
                    "topic": topic,
                    "start_line": start_line,
                    "max_tokens": max_tokens,
                }
                if end_line is not None:
                    arguments["end_line"] = end_line
                if token_offset:
                    arguments["token_offset"] = token_offset
                try:
                    result = self.source.read_sdk_reference(**arguments)
                except (HarnessMcpError, OSError) as error:
                    raise ProgramArtifactError(
                        f"serving SDK reference is unavailable: {error}"
                    ) from error
                document = result.structured
                if result.is_error:
                    reason = (
                        document.get("error") or document.get("message") or result.text
                    )
                    raise ProgramArtifactError(
                        f"SDK reference read failed: {str(reason)[:2000]}"
                    )
                if any(
                    document.get(key) != metadata[key]
                    for key in ("sdkIdentity", "sdkVersion", "sdkAbiVersion")
                ):
                    raise ProgramArtifactError(
                        "serving SDK reference is unavailable or changed"
                    )
                content = document.get("content")
                if not isinstance(content, str):
                    raise ProgramArtifactError("serving SDK reference is invalid")
                fields = (
                    "startLine",
                    "tokenOffset",
                    "endLine",
                    "totalLines",
                    "totalTokens",
                    "tokenizer",
                    "tokenCount",
                    "nextTokenOffset",
                    "complete",
                    "truncated",
                    "nextArguments",
                )
                missing = [key for key in fields if key not in document]
                if missing:
                    raise ProgramArtifactError(
                        f"serving SDK page is missing current token metadata {missing}; align the harness deployment"
                    )
                cost = count_tokens(json.dumps(content, ensure_ascii=False))
                next_offset = document["nextTokenOffset"]
                if (
                    document["tokenizer"] != TOKENIZER
                    or cost > max_tokens
                    or document["tokenCount"] != cost
                    or document["tokenOffset"] != token_offset
                    or type(document["totalTokens"]) is not int
                    or document["totalTokens"] < token_offset
                    or (
                        next_offset is not None
                        and (
                            type(next_offset) is not int
                            or not token_offset < next_offset <= document["totalTokens"]
                        )
                    )
                    or document["complete"]
                    != (token_offset == 0 and next_offset is None)
                    or document["truncated"]
                    != (token_offset != 0 or next_offset is not None)
                ):
                    raise ProgramArtifactError(
                        "serving SDK page has invalid token counts, cursor or completeness; align the harness deployment"
                    )
                cursor = document["nextArguments"]
                if (next_offset is None and cursor is not None) or (
                    next_offset is not None
                    and (
                        not isinstance(cursor, Mapping)
                        or cursor.get("token_offset") != next_offset
                        or cursor.get("topic") != topic
                        or cursor.get("start_line") != start_line
                        or cursor.get("max_tokens") != max_tokens
                    )
                ):
                    raise ProgramArtifactError(
                        "serving SDK page continuation does not match the selected source; align the harness deployment"
                    )
                return {
                    **metadata,
                    "topic": topic,
                    "source": content,
                    **{key: document[key] for key in fields},
                }
            metadata = self.matched_metadata()
            selected = topics[topic]
            path = (
                (self.root.parent.parent / "lake" / selected.removeprefix("lake-"))
                if selected.startswith("lake-contracts/")
                else self.root.parent / selected
            )
            text = path.read_bytes().decode("utf-8")
        else:
            raise ProgramArtifactError(
                f"unknown SDK reference topic {topic!r}; read topic='index' for available aliases and source paths"
            )
        try:
            page = source_page(
                text,
                topic,
                start_line=start_line,
                end_line=end_line,
                token_offset=token_offset,
                max_tokens=max_tokens,
            )
        except ValueError as error:
            raise ProgramArtifactError(str(error)) from error
        return {"topic": topic, **metadata, **page}

    def template_source(
        self, kind: ProgramKind, configuration: object, *, template: str = "basic"
    ) -> str:
        """Render the version-matched basic consumer for the exact declared inputs."""
        self.matched_metadata()
        if kind not in PROGRAM_TEMPLATES or template != "basic":
            raise ProgramArtifactError("unknown program template; use basic")
        check_configuration(configuration)
        subscriptions = cast(
            list[dict[str, object]],
            cast(Mapping[str, object], configuration)["subscriptions"],
        )
        declarations = []
        for item in subscriptions:
            variant = {"bars": "Bars", "quote": "Quote", "trade": "Trade"}[
                cast(str, item["data_type"])
            ]
            symbols = ", ".join(
                json.dumps(symbol) + ".into()"
                for symbol in cast(list[str], item["symbols"])
            )
            interval = (
                f", interval: {json.dumps(item['interval'])}.into()"
                if variant == "Bars"
                else ""
            )
            declarations.append(
                f"Subscription::{variant} {{ symbols: vec![{symbols}]{interval} }}"
            )
        source = PROGRAM_TEMPLATES[kind].replace(
            'vec![Subscription::Bars { symbols: vec!["SPY".into()], interval: "1min".into() }]',
            "vec![" + ", ".join(declarations) + "]",
        )
        if kind == "indicator" and any(
            item["data_type"] in {"trade", "quote"} for item in subscriptions
        ):
            source = source.replace("#[derive(Default)]\n", "")
            source += """
impl Default for AuthoredIndicator {
    fn default() -> Self {
        Self { last: BTreeMap::new(), sequence: IndicatorSequence::ticks(60_000_000_000).unwrap() }
    }
}
"""
        return source

    def local_identity(self) -> str:
        try:
            metadata = sdk_metadata(self.root)
            if metadata != sdk_metadata(identity.PACKAGED_SDK_ROOT):
                raise ProgramArtifactError(
                    "SDK reference/templates do not match the packaged SDK"
                )
            sources = source_hashes(self.root)
        except ProgramArtifactError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ProgramArtifactError(
                "SDK reference sources are unavailable; deploy the matching harness-strategy-sdk sources and configure the local SDK source root. Remote harness access does not supply local SDK references."
            ) from error
        return hashlib.sha256(canonical_bytes(sources)).hexdigest()
