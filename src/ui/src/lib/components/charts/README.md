# Evidence charts

These presentation components consume published evidence. They do not compute
financial statistics, choose experiments, or fill absent observations with zero.

- `HistogramChart.svelte` renders distributions supplied by report artifacts.
- `TimeSeriesChart.svelte` renders supplied time series.
- `ParameterBarChart.svelte` renders parameter assignments against total return
  and profit factor, with separate labeled scales and a readable values table.
- `parameter-bar.ts` projects decoded runs into marginal chart categories and
  supplies finite metric decoding and harness-state presentation. Category and
  differing-parameter comparisons consume backend canonical level identities;
  JavaScript numeric equality cannot preserve integer/float distinctions.

Research report composition belongs to the neighboring `chat` components;
accepted backend contracts remain the evidence authority.
