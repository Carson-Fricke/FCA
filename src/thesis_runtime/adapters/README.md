# thesis_runtime/adapters

Concrete persistence and model-provider implementations of runtime ports. Inner runtime layers do not choose these adapters.

Subdirectories: `postgres/`, `cosmos/`, `filesystem/`, `providers/`. Each documents its local boundary.

- `__init__.py`: adapters capability package.
- `persistence.py`: explicit configured backend selection and the shared PostgreSQL pool.
- `receipt_projection.py`: extension-independent PostgreSQL/Cosmos SELECT projection compilers and current chunked-receipt headers; callers select metadata names, never full result bodies.
