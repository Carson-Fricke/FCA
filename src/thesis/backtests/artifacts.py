"""Read harness run artifacts across local and remote topologies.

Local harness runs expose artifact bytes through the MCP ``read_artifact``
tool.  Remote (Azure) runs may refuse raw bytes over MCP and instead publish
the artifact through a SAS URL or an absolute object URL; this reader falls
back to a plain HTTP GET so one importer serves both topologies, which is what
gives the in-chat backtest card parity between local and Azure execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

import httpx

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.integrations.mcp import TOOL_LIST_ARTIFACTS, TOOL_READ_ARTIFACT

HttpGet = Callable[[str], bytes]


class ArtifactReader(Protocol):
    """Read one run artifact's bytes without assuming a storage backend."""

    def read(self, run_id: str, name: str) -> bytes | None: ...


class HarnessArtifactReader:
    """Read artifacts via MCP first, then a SAS/object URL over HTTP.

    The MCP ``read_artifact`` tool works for local runs.  A remote harness may
    reject it; in that case the artifact's published ``sas_url`` (or an absolute
    ``uri``) is fetched directly, which is the documented remote access path.
    """

    def __init__(
        self,
        tools: HarnessToolProvider,
        *,
        http_get: HttpGet | None = None,
    ) -> None:
        self._tools = tools
        self._http_get: HttpGet = (
            http_get if http_get is not None else _default_http_get
        )

    def read(self, run_id: str, name: str) -> bytes | None:
        via_mcp = self._read_via_mcp(run_id, name)
        if via_mcp is not None:
            return via_mcp
        url = self._artifact_url(run_id, name)
        if url is None:
            return None
        try:
            return self._http_get(url)
        except Exception:  # an unreachable artifact must never fail the import
            return None

    def _read_via_mcp(self, run_id: str, name: str) -> bytes | None:
        result = self._tools.call_tool(
            TOOL_READ_ARTIFACT, {"run_id": run_id, "name": name}
        )
        if not result.ok:
            return None
        return _decode(result.structured)

    def _artifact_url(self, run_id: str, name: str) -> str | None:
        result = self._tools.call_tool(TOOL_LIST_ARTIFACTS, {"run_id": run_id})
        if not result.ok:
            return None
        entries = result.structured.get("artifacts")
        if not isinstance(entries, (list, tuple)):
            return None
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("name") != name:
                continue
            for key in ("sas_url", "sasUrl", "uri"):
                value = entry.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
        return None


def _decode(structured: Mapping[str, object]) -> bytes | None:
    content = structured.get("content")
    if not isinstance(content, str):
        return None
    encoding = structured.get("encoding")
    if isinstance(encoding, str) and encoding.lower() == "hex":
        try:
            return bytes.fromhex(content)
        except ValueError:
            return None
    return content.encode("utf-8")


def _default_http_get(url: str) -> bytes:
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return response.content


__all__ = [
    "ArtifactReader",
    "HarnessArtifactReader",
]
