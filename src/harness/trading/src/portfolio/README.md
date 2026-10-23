# portfolio

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `book.rs`: Decimal-exact accounting for one strategy's virtual book.
- `hub.rs`: Multiple virtual books and their shared-account aggregate.
- `mod.rs`: Virtual-book and shared-account portfolio accounting.
- `options_lifecycle.rs`: Deterministic option expiry, exercise, and assignment settlement.
