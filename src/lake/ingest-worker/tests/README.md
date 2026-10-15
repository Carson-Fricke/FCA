# Finite ingestion behavior

- `local.rs`: runs a real finite worker against a fake provider, verifies trades/quotes/bars publication and durable ownership, exercises local HTTP admission and scoped downloads, and compares native/HTTP decoded output. It never contacts Azure or a real provider.
