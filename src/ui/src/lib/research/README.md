# Published research exhibits

This directory renders immutable job evidence. It depends on the API and
canonical transcript types; it cannot author studies, submit jobs or send agent
messages. Chat and the workspace explorer use the same component.

- `contracts.ts`: strict versioned descriptor decoding, scalar evidence and
  explicit identity/type/causal-time validation. Nulls remain missing.
- `semantics.ts`: pure publisher-supplied output interpretation, recommendations,
  field candidates, explicit filtering, and ordered display projections.
- `policies.ts`: per-grammar field-role and coordinate policies; duplicate cells
  are rejected without client-side aggregation.
- `EvidencePanel.svelte`: registered renderers, linked selection/cursor state,
  source inspection, missing-value and bounded-preview notices.
- `ReturnedData.svelte`: recommended and custom mappings, dimension filters,
  searchable paginated tables, and explicit loaded-data coverage.
- `resources.ts`: shared immutable page cache, artifact identity checks,
  cancellation and bounded paging (5,000 rows or approximately 2 MB per dataset).
- `registry.ts`: local component registration and required field mappings for
  visual primitives; evidence never supplies executable components.
- `ResearchExplorer.svelte`: compact tabs, lazily loaded output evidence,
  standalone dataset semantics, all raw outputs and receipt datasets, in-place
  expansion, provenance and full-artifact export. Awaiting execution defers reads.
- `jobs.ts`: one observational status poller per backend/job while subscribed.
  It stops on terminal status or unmount and cannot resubmit executions.
- `grouping.ts`: pure, scope-local presentation grouping of study observations
  and comparison with previously displayed sketch revisions. Receipts remain
  canonical and are retained in the grouped view.
- `panels/`: registered evidence renderers and display coordinate math.

## Extension contract

Study authors publish `StudyOutput::ResearchView` alongside scalar tables,
series or events. Descriptors name outputs in the same program instance and
job, fields and units, and optional explicit linked identities and causal
timestamps. The JIT host validates the complete publication at stop. Readers
independently validate untrusted descriptors and bounded evidence pages.

Most new methods need only datasets and a composition of registered primitives.
Specialized visual grammars register a versioned panel kind and a local Svelte
component implementing `PanelProps`. Unknown kinds expose their evidence.
Adding a method does not require a tool-name conditional or another transport.

Heatmap rows are discrete bands; sequence paths respect published order,
separate declared series and break at nulls. Distribution panels display
published bins; they do not calculate financial statistics in the browser.
Linked selections compare declared identities within an explicit group;
linked cursors compare declared event coordinates. No positional joins exist.

`/demo/research` exercises all nine family recipes from a validated SDK example,
paged through the production Python projection. The data is explicitly synthetic.
Outputs without dataset semantics retain exact tables and saved view mappings;
custom axes require explicit roles. Standalone `StudyOutput::DatasetSemantics`
describes row meaning, grain, dimensions, measures, ordered coordinates and
preferred views independently of an exhibit. The SDK validates grain over the
complete publication; browser checks and filters apply only to loaded evidence.
New visual grammars register a component, mappings and optional `MappingPolicy`.
No execution or durable-message delivery path is introduced.
