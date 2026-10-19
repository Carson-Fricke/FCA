# Exact results and bounded publication

The result layer owns numerical summaries, reporting and recorder composition;
it does not acquire market data or admit jobs.

- `mod.rs`: exports the result modules.
- `metrics.rs`: computes trade and equity statistics, including repeated streaming passes over full-resolution spilled equity histories.
- `report.rs`: renders reports from supplied results; visualization points may be bounded without changing financial statistics.
- `recorder/`: writes schema-checked Parquet batches and artifact manifests.
