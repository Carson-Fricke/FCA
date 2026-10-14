# thesis_runtime/storage

Storage ownership contracts and local mutation locks. Concrete durable ownership adapters belong under adapters.

- `__init__.py`: storage capability package.
- `locks.py`: Shares local per-conversation mutation locks without making persistence adapters depend on one another.
- `ownership.py`: Renewable ownership and ambient write fencing, independent of execution policy.
