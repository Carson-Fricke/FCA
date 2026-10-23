# src

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `fleet.rs`: Owns bounded worker reservations, attachment, renewal and release.
- `jobs.rs`: Owns neutral job records, admission/publication invariants and storage capabilities.
- `lib.rs`: Owns job identity, lifecycle and fenced execution contracts.
- `persistence.rs`: Conditional persistence required by fenced execution.
- `store.rs`: One current durable job contract, shared by APIs and fenced finite workers.
