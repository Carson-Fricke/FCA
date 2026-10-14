---
id: publishing-research-exhibits
title: Publish modular research exhibits from study evidence
version: 3
created: 2026-10-09
retrieved: 2026-10-12
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 1dbf2452da81baa652ffdc2d937980ed897139f5a9ea29c90e54bc21415e326c
tags:
  - discovery
  - sdk
  - visualization
  - evidence
collections:
  - discovery
  - model-cards
links:
  - executable-discovery-tool-bag
  - discovery-study-design
relationships:
  - type: relates
    target: executable-discovery-tool-bag
trust:
  tier: curated
  state: published
---
# Publish inspectable evidence alongside the research argument

The serving SDK is authoritative. Read `research_views`, `research_publishing`,
and `research_recipes` with `read_sdk_reference` before authoring. New methods
use this common publication contract; they do not require a method-specific
chat tool or frontend renderer.

Publish `StudyOutput::Table`, `Series`, or `Events` with the actual observations,
and `StudyOutput::DatasetSemantics { semantics }` explaining their meaning. A
separate presentation selector recommends several visualizations using complete
field profiles. Authors do not need to choose chart types or axes. An optional
`StudyOutput::ResearchView { view }` may still describe a deliberate composition. Each dataset
binds a named output in the same program instance and originating job. Declare
field names, human labels, nonempty scientific `description`, scalar types and units. Explain
what each dimension varies and each measure records, including parameter versus
observation identity and the units of horizons and supporting counts. Declare observation identity,
selection group, event time and availability explicitly when applicable.
The host validates the complete publication after stop callbacks, so a view
cannot silently reference absent evidence or nonunique observation identities.

`EvidenceTable` accepts rectangular scalar rows; `publish_described(semantics, publisher)`
publishes the table and its meaning together. `WaveletHistory::observe`
records actual trailing Haar coefficients and per-scale mean squared detail
energy from supplied trailing values. It preserves exact timestamp strings;
authors supply session/symbol identity and choose a bounded observation cadence.
It does not recreate missing historical coefficients or compute MODWT/CWT.

Use `research::recipes::compose(family, title, datasets, fields)` for a family
composition. `datasets` maps semantic slots to explicit `Dataset` descriptors;
`fields` maps `slot.role` to actual field names, for example `energy.time` to
`event_time_ns`. Retrieve `recipe(family)` to see the required slots and roles.
These are composition helpers, not research implementations:

| Family | Published evidence used by the composition |
| --- | --- |
| `wavelets` | Time-scale energy, price context, session scale proportions/outcomes |
| `seasonality` | Session clock grid and separately identified intraday paths |
| `events` | Confirmed event locations, duration/magnitude and aligned paths |
| `regimes` | Membership intervals, transition probabilities and dwell bins |
| `predictive_signals` | Observations, horizon estimates, calibration and stability |
| `liquidity` | Synchronized quote fields and execution-cost bins |
| `parameters` | Joint parameter outcomes and experiment records, including statuses |
| `falsification` | Claim/test relationships, null bins, controls and subsamples |
| `learned_models` | Declared training windows, feature weights, diagnostics and predictions |

For another method, construct `ResearchView` directly from registered panel
kinds: `table`, `line`, `heatmap`, `scatter`, `comparison`, `distribution`,
`events`, `intervals`, `composition`, and `graph`. The panel encodings map visual
channels to declared fields. A genuinely new visual grammar needs a locally
registered versioned panel component; unknown kinds leave evidence inspectable.
Never publish executable HTML/JavaScript as a rendering instruction.

Publish distributions, confidence intervals, tests, transitions, fitted state
and outcomes in the study itself. The UI renders evidence; it does not estimate
financial statistics, infer market sessions or silently fit models. Preserve
nulls, warm-up reasons, failures, untested parameter combinations, and limits of
interpretation. No coefficient history, clustering algorithm or robustness
test exists merely because its panel could display one.

Linked selections require equal explicit identities in a common selection
group; dataset identities are unique. Do not join by row position. Different
granularities can share declared event coordinates for linked cursors without
pretending observations are identical. Include evidence provenance and specify
what is absent in `view.limitations`. Inspect full artifact exports when a view
shows its bounded preview warning (5,000 rows or approximately 2 MB per dataset).

Historical jobs without a saved composition use explicit field-selectable
previews. Producing richer evidence requires an intentionally authored and
executed new study; presentation never silently reruns a historical execution.


## Declare meaning independently of a research exhibit

For chartable evidence, publish `StudyOutput::DatasetSemantics { semantics }` with
`research::DatasetSemantics`. Its schema version is 1. Supply `dataset`, a human
`row_meaning`, the complete nonnull composite `grain`, `dimensions`, numeric
`measures`, numeric/timestamp `ordered` dimensions, and `preferred_views: vec![]`.
Keep that list empty for ordinary study authoring; the presentation selector owns
chart choice. Each `Field` requires a meaningful `description`.
Identifiers and supporting counts are not interchangeable with analytical axes.
A numeric parameter can be a quantitative scatter coordinate while remaining a
dimension; a numeric identifier cannot. Describe one output once per instance.
A research view binding for that output must equal the canonical dataset binding.

The host validates grain uniqueness against the complete output. This descriptor
works without `ResearchView`; preferred views can recommend a raw-data chart.
The backend profiles complete output ranges, cardinality, missingness, quartiles,
and distributions, then asks Decider to rank locally generated valid plans.
The Returned data tab permits all manual field combinations, optional color and
series, numeric/timestamp buckets and explicitly chosen cell reductions. It never
silently averages or infers statistical meaning. Precompute scientific
aggregates in the study and publish their actual row grain and units.

Filtering and mapping checks cover the explicitly labeled loaded preview, not
unloaded observations. Export retains the complete immutable artifact. Bare
outputs without semantics retain exact table/state inspection and manual plots.
Their observed shape supports basic recommendations, explicitly marked as lacking
published scientific meaning; no scientific descriptions are invented.
