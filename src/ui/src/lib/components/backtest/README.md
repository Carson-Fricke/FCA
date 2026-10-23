# Published backtest views

`SplitEquityView.svelte` displays the report's equity/trades and an explicitly
declared train/test split when present. Its summary reads canonical snake_case
published metric fields. Missing equity never prevents published metrics from
rendering, and chart samples never determine headline financial statistics.
