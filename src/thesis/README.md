# thesis

FCA application, domain services, concrete tool/workflow extensions and HTTP transport. Generic execution belongs to the peer thesis_runtime package.

Subdirectories: `activation/`, `agent/`, `application/`, `artifacts/`, `auth/`, `backtests/`, `experiments/`, `extensions/`, `http/`, `identity/`, `integrations/`, `knowledge/`, `operations/`, `persistence/`, `protected/`, `rca/`, `registry/`, `research/`, `screening/`, `strategies/`, `workflows/`, `workspaces/`. Each documents its local boundary.

- `__init__.py`: FCA Thesis workspace research platform.
- `__main__.py`: Module entrypoint for the Thesis backend command line.
- `cli.py`: Thesis backend command-line facade and process boundary.
- `configuration.py`: Loader for the repository's non-executable YAML configuration subset.
- `paths.py`: The portable relative-path rule shared by authored workspace artifacts.
