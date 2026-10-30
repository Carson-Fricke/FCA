"""Generate browser demonstration pages from validated Rust SDK publications."""

import hashlib
import json
import subprocess
from pathlib import Path

from thesis.research.presentation import PresentationService
from thesis.workspaces.exploration import output_page


class PreviewRanker:
    """Exercise the planner with no paid or network model invocation."""

    model = "deterministic-preview"

    def rank(self, state, candidates):
        raise RuntimeError("Synthetic preview; no model call was made.")


def main() -> None:
    content = subprocess.check_output(
        [
            "cargo",
            "run",
            "--quiet",
            "--manifest-path",
            "src/harness/Cargo.toml",
            "-p",
            "harness-strategy-sdk",
            "--example",
            "research_gallery",
        ],
        text=True,
        encoding="utf-8",
    )
    document = json.loads(content)
    job_id = document["job_id"]
    pages = {}
    catalog = output_page(job_id, content)
    for instance, outputs in document["programs"].items():
        for name in outputs:
            offset = 0
            while True:
                page = output_page(
                    job_id,
                    content,
                    instance_id=instance,
                    output_name=name,
                    offset=offset,
                )
                pages[f"{instance}/{name}/{offset}"] = page
                next_offset = page["nextOffset"]
                if next_offset is None:
                    break
                if not isinstance(next_offset, int):
                    raise TypeError("Expected an integral evidence cursor")
                offset = next_offset
    destination = Path("src/ui/src/lib/fixtures/research-gallery.json")
    destination.write_text(
        json.dumps(
            {
                "generatedBy": "scripts/fixtures/generate_research_gallery.py",
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "catalog": catalog,
                "artifact": document,
                "pages": pages,
                "recommendations": PresentationService(PreviewRanker()).recommend(
                    job_id, content
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
