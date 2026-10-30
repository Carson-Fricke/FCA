"""Storage projections for read-only navigation, independent of extensions.

Callers select structured metadata names; adapters project those facts directly
from canonical messages. No body, tool text, reasoning, continuation or delivery
state is reconstructed. Chunk hydration uses this same explicit projection.
"""

import re
from collections.abc import Mapping
from typing import Any


def metadata_name(name: str) -> str:
    """Validate an application-owned JSON property before compiling a projection."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("metadata projection names must be JSON identifiers")
    return name


def receipt_header(
    document: Mapping[str, Any],
    fields: tuple[str, ...],
    arrays: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Project a hydrated current receipt; required canonical facts stay required."""
    message = document["payload"]
    session = message["data"]["sessionMessage"]
    content = session["content"]
    result = content.get("result") or {}
    indexed = "datums" in result
    datums = (
        result.get("datums", ())
        if indexed
        else (result,)
        if "datumType" in result
        else ()
    )

    def metadata(data: Mapping[str, Any]) -> dict[str, Any]:
        projected = {key: data[key] for key in fields if key in data}
        for key, child in arrays:
            projected[key] = [
                {child: item[child]} for item in data.get(key, ()) if child in item
            ]
        return projected

    return {
        "seq": document["seq"],
        "threadId": document.get("threadId") or "root",
        "messageId": message["message_id"],
        "executionId": message["execution_id"],
        "role": message["role"],
        "title": message["body"][:160],
        "direction": session["direction"],
        "hasRequests": bool(content.get("requests")),
        "requestMessageId": (content.get("correlation") or {}).get(
            "request_message_id"
        ),
        "indexed": indexed,
        "arguments": metadata(result.get("arguments") or {}),
        "datums": [
            {
                "datumType": datum["datumType"],
                "ok": datum["ok"],
                "structured": metadata(datum.get("structured") or {}),
            }
            for datum in datums
        ],
    }


def postgres_receipt_header(
    fields: tuple[str, ...], arrays: tuple[tuple[str, str], ...]
) -> str:
    """Compile SELECT columns; names are validated constants, values stay in JSONB."""
    fields = tuple(metadata_name(field) for field in fields)
    arrays = tuple((metadata_name(key), metadata_name(child)) for key, child in arrays)

    def metadata(expr: str, *, collections: bool = True) -> str:
        pairs = [f"'{key}',{expr}->'{key}'" for key in fields]
        if collections:
            pairs += [
                f"'{key}',(SELECT COALESCE(jsonb_agg(jsonb_build_object('{child}',entry->'{child}')),'[]') FROM jsonb_array_elements(COALESCE({expr}->'{key}','[]')) entry)"
                for key, child in arrays
            ]
        return "jsonb_build_object(" + ",".join(pairs) + ")"

    content = "body#>'{payload,data,sessionMessage,content}'"
    result = f"({content})->'result'"
    indexed = f"jsonb_typeof(({result})->'datums')='array'"
    datums = f"CASE WHEN {indexed} THEN ({result})->'datums' WHEN ({result}) ? 'datumType' THEN jsonb_build_array({result}) ELSE '[]'::jsonb END"
    return (
        "jsonb_build_object('seq',seq,'threadId',thread_id,'messageId',body#>'{payload,message_id}',"
        "'executionId',body#>'{payload,execution_id}','role',body#>'{payload,role}',"
        "'title',left(body#>>'{payload,body}',160),'direction',body#>'{payload,data,sessionMessage,direction}',"
        f"'hasRequests',jsonb_array_length(COALESCE(({content})->'requests','[]'))>0,"
        f"'requestMessageId',({content})->'correlation'->'request_message_id','indexed',COALESCE({indexed},false),"
        f"'arguments',{metadata(f"({result})->'arguments'", collections=False)},"
        f"'datums',(SELECT COALESCE(jsonb_agg(jsonb_build_object('datumType',datum->'datumType','ok',datum->'ok','structured',{metadata("(datum)->'structured'")})),'[]') FROM jsonb_array_elements({datums}) datum))"
    )


def cosmos_receipt_header(
    fields: tuple[str, ...], arrays: tuple[tuple[str, str], ...]
) -> str:
    """Compile a bounded Cosmos projection over the inline current representation."""

    def metadata(expr: str, *, collections: bool = True) -> str:
        pairs = [f'"{metadata_name(key)}":{expr}.{key}' for key in fields]
        if collections:
            pairs += [
                f'"{metadata_name(key)}":ARRAY(SELECT VALUE {{"{metadata_name(child)}":entry.{child}}} FROM entry IN {expr}.{key})'
                for key, child in arrays
            ]
        return "{" + ",".join(pairs) + "}"

    content = "c.payload.data.sessionMessage.content"
    result = content + ".result"

    def datum(expr: str) -> str:
        return f'{{"datumType":{expr}.datumType,"ok":{expr}.ok,"structured":{metadata(expr + ".structured")}}}'

    return (
        '{"seq":c.seq,"threadId":c.threadId,"messageId":c.payload.message_id,"executionId":c.payload.execution_id,'
        '"role":c.payload.role,"title":SUBSTRING(c.payload.body,0,160),"direction":c.payload.data.sessionMessage.direction,'
        f'"hasRequests":ARRAY_LENGTH({content}.requests)>0,"requestMessageId":IIF(IS_DEFINED({content}.correlation.request_message_id),{content}.correlation.request_message_id,null),'
        f'"indexed":IS_ARRAY({result}.datums),"arguments":{metadata(result + ".arguments", collections=False)},'
        f'"datums":IIF(IS_ARRAY({result}.datums),ARRAY(SELECT VALUE {datum("datum")} FROM datum IN {result}.datums),IIF(IS_DEFINED({result}.datumType),[{datum(result)}],[]))}}'
    )
