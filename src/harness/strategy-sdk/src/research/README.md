# Publish modular research exhibits

This module depends on SDK output contracts and existing pure kernels. It has
no engine, file, network or order capabilities.

- `mod.rs`: public facade and complete-publication validation.
- `view.rs`: version 1 strict dataset, field, panel and view contracts; validates
  same-instance references, scalar types, unique identities and causal timing.
  `DatasetSemantics` independently declares row meaning, canonical grain, field
  roles, ordering and preferred views. Complete-publication validation checks
  grain uniqueness and agreement between semantic and exhibit bindings.
- `publishing.rs`: rectangular evidence tables and actual trailing Haar
  coefficient/energy histories. Authors choose observation cadence and budgets.
- `recipes.rs`: reusable panel compositions for nine study families; these
  compose already published evidence and do not implement analysis algorithms.

Publish tables/series/events first or later in the same stop callback, then a
`StudyOutput::ResearchView`. The host validates all bindings after callbacks.
Declare exact timestamp strings in evidence tables, units and missing states.
To link panels, declare a common selection group and equal explicit identities.
Identity is unique within a dataset; row position never defines a join.

Read `research_views`, `research_publishing`, and `research_recipes` through
`read_sdk_reference` for serving-version signatures. The sibling
`examples/research_gallery.rs` demonstrates complete valid publications.

Publish `StudyOutput::DatasetSemantics { semantics }` for independently chartable
outputs, including those without an exhibit. Its schema version is 1. Roles refer
to declared fields; measures are numeric, identifiers are excluded from dimensions
and measures, and ordered dimensions are numeric or timestamps. Grain is a nonnull
unique composite key over the complete output. No implicit aggregation is defined.
