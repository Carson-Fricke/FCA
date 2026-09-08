# SPY 0DTE Flat-IV Theta/Delta Experiment

Local-only experiment testing whether 0DTE option theta and delta behavior
match the flat-IV model uniformly through the trading day.

## Layout

- `osi.py` — session enumeration, 0DTE OSI ladder construction, lake-schema
  Parquet helpers, experiment state.
- `ingest.py` — real Alpaca pull: SPY equity trades/quotes (SIP) plus
  expiration-day option quotes/trades (OPRA, enforced by the client), tick
  level, no downsampling, resumable via `state.json`.
- `synthetic.py` — ground-truth generator: same lake layout with a known
  theta-warp (`--mode warp`) or IV-schedule (`--mode iv`) injected.
- `analyze.py` — full flat-IV decomposition: four-corner repricing, residual
  (IV effect), implied-time clock ratio, per-bucket regressions.
- `open_iv.py` — the simple headline test: 20-second IV path from the open;
  flat path = theta slower than model, marked drop = theta faster.

Data roots used below: `.local/spy-0dte/lake` (real) and
`.local/spy-0dte-synthetic/lake` (validation). Everything is gitignored
generated data.

## Validation (already run)

- Decomposition (`analyze.py`, synthetic warp mode): recovers injected
  theta multiples per bucket (morning 1.25 -> 1.254, midday 1.0 -> 1.009,
  afternoon 0.75 -> 0.778 ATM; delta multiple ~1.0 everywhere).
- Open-IV (`open_iv.py`, warp mode): path flat through the opening window
  (0/4 days with drop) — no false positives.
- Open-IV (`open_iv.py`, iv mode): recovers the injected IV schedule to
  within ~0.15 IV-ratio points — drops are detected when real.

## Real-data run

```powershell
$env:LAKE_PROVIDER__API_KEY = "..."
$env:LAKE_PROVIDER__API_SECRET = "..."
uv run --extra dev python scripts/experiments/spy_0dte/ingest.py --days 42
uv run --extra dev python scripts/experiments/spy_0dte/open_iv.py `
    --data-root .local/spy-0dte/lake --out .local/spy-0dte/open-iv
uv run --extra dev python scripts/experiments/spy_0dte/analyze.py `
    --data-root .local/spy-0dte/lake --out .local/spy-0dte/analysis
```

Ingest is resumable; re-run the same command after interruption. Requires an
Alpaca subscription with historical OPRA data (Algo Trader Plus).

## Interpretation notes

- The IV path and the implied-time clock ratio are two views of the same
  drift; the decomposition residual separates the IV component from the
  time component of observed price changes.
- Quote granularity is $0.01: buckets where the option value approaches a
  few cents (power hour, wings) are noise-attenuated. `--interval-minutes 5`
  on `analyze.py` raises signal-to-noise at the cost of observations.
- `--rate` and `--dividend-yield` are fixed constants; ex-dividend sessions
  should be excluded before drawing conclusions from deep-ITM calls.
