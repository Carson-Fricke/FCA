# Backend evidence fixtures

`research-reports.json` is produced by the accepted Python research contracts
and the backend report artifact projection. The report UI tests consume it
without independently authoring financial values or attribution.

Regenerate from the repository root with:

`uv run --extra dev python -m tests.thesis.workflows.registry.hypothesis_development.test_report_artifacts`

The corresponding Python test verifies that the fixture has not drifted from
the current backend projection.
