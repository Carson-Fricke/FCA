# thesis/extensions/tools

Concrete FCA workspace, program, sketch, retrieval, exploration, strategy and harness tool composition. Handlers receive trusted context separately from model arguments.

- `__init__.py`: tools capability package.
- `artifacts.py`: Bounds agent-facing artifact pages with character offsets and optional JSON-pointer selection before inbox delivery. The harness's full-file port remains available to deterministic readers; artifacts are never rewritten.
- `exploration.py`: Inspect canonical exploratory outputs through ordinary session tool dispatch, checking the workflow's window prerequisite when supplied.
- `knowledge.py`: Bind knowledge retrieval to runtime-owned workspace and source authority.
- `program.py`: Adapt committed program operations and job-pinned executed-study/strategy reads to ordinary, runtime-attributed tools. All workflow calls check the declared-window prerequisite without hiding tools; execution passes the resolved policy to the program service before allocation or dispatch.
- `registry.py`: Compose one merged tool catalog, including scoped workflow-owned tools, for an agent session; project job metadata with accepted-input references instead of inline source.
- `sketch.py`: Runtime-scoped authoring and inspection of committed strategy sketches, checking the workflow's window prerequisite when supplied.
- `strategy.py`: Adapt an injected strategy authoring service to trusted session tools.
