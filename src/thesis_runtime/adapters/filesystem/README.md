# thesis_runtime/adapters/filesystem

Local JSON persistence for neutral runtime records. The common codec belongs to contracts rather than this adapter.

- `__init__.py`: filesystem capability package.
- `records.py`: Filesystem-backed record admission, locked root updates, and ordered input windows for single-process restart tests; local record scans are intentional and no distributed work queue is hosted here.
