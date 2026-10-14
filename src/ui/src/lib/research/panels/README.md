# Evidence panel renderers

These components depend on research contracts and receive shared selection and
cursor state from the explorer. They project published evidence only.

- `PlotPanel.svelte`: accessible SVG heatmaps, sequences, points, published
  bins/comparisons, events, intervals, compositions and relationship graphs.
- `TablePanel.svelte`: bounded incremental record rendering and identity selection.
- `plot.ts`: pure display axes, exact timestamp deltas, null path breaks,
  series grouping and geometric coordinates; no statistical estimation.
