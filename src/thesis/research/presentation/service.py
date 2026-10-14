"""Cached study presentation planning; no execution or agent transcript writes."""

import hashlib
from collections import OrderedDict
from threading import Lock
from typing import Any, Protocol

from thesis.research.presentation.candidates import candidates_for
from thesis.research.presentation.decisions import DecisionRanker
from thesis.research.presentation.profiles import datasets


class Ranker(Protocol):
    model: str

    def rank(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> tuple[dict[str, float], dict[str, Any]]: ...


class PresentationService:
    """One replaceable decision port over full-data profiles and fixed candidate IDs."""

    def __init__(self, ranker: Ranker | None = None) -> None:
        self._ranker = ranker or DecisionRanker()
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = Lock()

    def recommend(self, job_id: str, content: str) -> dict[str, Any]:
        sha = hashlib.sha256(content.encode()).hexdigest()
        cache_key = f"planner-1:{self._ranker.model}:{sha}"
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
            data = datasets(job_id, content)
            plans = {(d["instanceId"], d["name"]): candidates_for(d) for d in data}
            # Round-robin outputs as well as grammars before the request budget.
            candidates = [
                p
                for i in range(64)
                for group in plans.values()
                if i < len(group)
                for p in [group[i]]
            ][:64]
            public = [{k: v for k, v in d.items() if k != "rows"} for d in data]
            scores: dict[str, float] = {}
            receipt: dict[str, Any] = {"model": self._ranker.model}
            status = "deterministic"
            note = "No chart candidates are supported by this output."
            if candidates:
                try:
                    scores, receipt = self._ranker.rank(
                        {"datasets": public}, candidates
                    )
                    status, note = (
                        "ranked",
                        "Recommendations ranked from complete output profiles.",
                    )
                except (RuntimeError, ValueError) as error:
                    note = (
                        str(error)
                        if isinstance(error, RuntimeError)
                        else "Decision provider unavailable; showing deterministic suggestions."
                    )
            for d in public:
                options = plans[(d["instanceId"], d["name"])]
                ranked = [p for p in options if p["id"] in scores]
                d["recommendation_status"] = "ranked" if ranked else "deterministic"
                ordered = sorted(
                    enumerate(ranked or options),
                    key=lambda item: (-scores.get(item[1]["id"], -1), item[0]),
                )
                # Preserve useful alternatives instead of filling every tab with one grammar.
                selected = []
                kinds = set()
                for _, p in ordered:
                    if p["kind"] not in kinds:
                        selected.append(p)
                        kinds.add(p["kind"])
                    if len(selected) == 3:
                        break
                d["panels"] = selected
                d["candidates"] = options
            result = {
                "job_id": job_id,
                "artifactSha256": sha,
                "status": status,
                "note": note,
                "receipt": receipt,
                "datasets": public,
            }
            # Failed calls are not cached; one explicit refresh may try again.
            if status == "ranked" or not candidates:
                self._cache[cache_key] = result
                while len(self._cache) > 32:
                    self._cache.popitem(last=False)
            return result
