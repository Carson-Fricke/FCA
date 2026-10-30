"""Token-budgeted source ranges for SDK guidance and committed programs.

Callers own source identity, resource selection and authorization. This module
owns LF line selection and the same cl100k_base budget/cursors as the Rust reader.
"""

from __future__ import annotations

from thesis_runtime.tools.reading import DEFAULT_READ_TOKENS, text_page


def source_page(
    text: str,
    topic: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    token_offset: int = 0,
    max_tokens: int = DEFAULT_READ_TOKENS,
) -> dict[str, object]:
    """Return the whole selected range when it fits, with lossless continuation."""
    text = text.replace("\r\n", "\n")
    chunks = text.split("\n")
    lines = [line + "\n" for line in chunks[:-1]]
    if chunks[-1]:
        lines.append(chunks[-1])
    end = len(lines) if end_line is None else end_line
    if (
        type(start_line) is not int
        or type(end) is not int
        or not 1 <= start_line <= end <= len(lines)
    ):
        raise ValueError(
            f"source range must satisfy 1 <= start_line <= end_line <= {len(lines)}; omit end_line to read to the end"
        )
    page = text_page(
        "".join(lines[start_line - 1 : end]),
        token_offset=token_offset,
        max_tokens=max_tokens,
    )
    next_offset = page["nextTokenOffset"]
    source = page.pop("content")
    return {
        **page,
        "source": source,
        "startLine": start_line,
        "endLine": end,
        "totalLines": len(lines),
        "nextArguments": {
            "topic": topic,
            "start_line": start_line,
            "end_line": end,
            "token_offset": next_offset,
            "max_tokens": max_tokens,
        }
        if next_offset is not None
        else None,
    }
