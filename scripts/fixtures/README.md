# Generate deterministic presentation fixtures

- `generate_research_gallery.py`: runs the validated Rust SDK example and pages
  its artifact through `thesis.workspaces.exploration.output_page`, retaining the
  exact artifact and SHA for the browser gallery. Run with `uv run --extra dev
  python scripts/fixtures/generate_research_gallery.py` from the repository root.
