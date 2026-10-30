"""Optional TypeSafe evidence ranking; never writes a continuation summary.

Requests rank bounded excerpts of whole canonical groups. The session supplies
an audit boundary before every external call. The working model independently
receives the complete retiring transcript. Credentials stay at this adapter.
"""

import json
from collections.abc import Mapping, Sequence
from math import isfinite

import httpx

from thesis_runtime.contracts.compaction import EvidenceAudit
from thesis_runtime.contracts.messages import SessionMessage


class JevEvidenceRanker:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._client = client

    def rank(
        self,
        groups: Sequence[tuple[SessionMessage, ...]],
        *,
        focus: str,
        audit: EvidenceAudit,
    ) -> Sequence[int]:
        scores: list[tuple[float, int]] = []
        # Bounded advisory requests, independent questions, no transcript pruning.
        for start in range(0, len(groups), 16):
            batch = groups[start : start + 16]
            request: Mapping[str, object] = {
                "model": "jev-latest",
                "state": {
                    "task_focus": focus[:8_000],
                    "groups": {
                        str(start + index): json.dumps(
                            [row.as_dict() for row in group], ensure_ascii=False
                        )[:8_000]
                        for index, group in enumerate(batch)
                    },
                },
                "questions": {
                    str(start + index): {
                        "type": "noul",
                        "instructions": (
                            f"Group {start + index} contains exact evidence, identifiers, constraints, "
                            "or unresolved failure details that should be retained verbatim alongside "
                            "a task summary for continued work. Routine status chatter and easily "
                            "recoverable details do not require verbatim retention."
                        ),
                    }
                    for index in range(len(batch))
                },
            }

            def send(request: Mapping[str, object] = request) -> Mapping[str, object]:
                try:
                    post = self._client.post if self._client is not None else httpx.post
                    response = post(
                        "https://api.typesafe.ai/v1/systemone",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=dict(request),
                        timeout=20,
                    )
                    response.raise_for_status()
                    value = response.json()
                except (httpx.HTTPError, ValueError) as error:
                    raise OSError(
                        "Optional Jev evidence selection was unavailable"
                    ) from error
                if not isinstance(value, Mapping):
                    raise ValueError("invalid Jev response")
                return value

            response = audit(
                tuple(row for group in batch for row in group), request, send
            )
            answers = response.get("answers")
            if not isinstance(answers, Mapping):
                raise ValueError("Jev response omitted answers")
            for index in range(start, start + len(batch)):
                answer = answers.get(str(index))
                value = answer.get("noul") if isinstance(answer, Mapping) else None
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not isfinite(value)
                    or not 0 <= value <= 1
                ):
                    raise ValueError("Jev response omitted a valid evidence score")
                if value >= 0.5:
                    scores.append((float(value), index))
        return tuple(
            index for _, index in sorted(scores, key=lambda item: (-item[0], item[1]))
        )
