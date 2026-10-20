# Exact results and bounded publication

The result layer owns numerical summaries, reporting and recorder composition;
it does not acquire market data or admit jobs.

- `mod.rs`: exports the result modules.
- `metrics.rs`: computes trade and equity statistics, with an incremental first pass and a second pass shared with artifact encoding; all full-resolution samples remain exact.
- `report.rs`: renders reports from supplied results; visualization points may be bounded without changing financial statistics.
- `recorder/`: writes schema-checked Parquet batches and artifact manifests.
