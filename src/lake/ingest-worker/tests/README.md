# Finite ingestion behavior

- `local.rs`: runs concurrent finite worker processes against a fake provider, proves overlapping partition acquisition under one retained job ID, verifies trades/quotes/bars publication and durable ownership, exercises local HTTP admission and scoped downloads, and compares native/HTTP decoded output. It never contacts Azure or a real provider.
- `unit/` contains the private monotonic permit lifetime check.
