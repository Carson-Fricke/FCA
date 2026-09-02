# Alpaca Delisted-History Coverage Sweep

## Scope

This memo records the H9.2 probe design and the current evidence boundary for
30 representative symbols spanning acquisition, bankruptcy, delisting, and
symbol-reuse cases. The implementation is
`src/lake/tools/delisted_probe.py`. It calls the existing
`lake.providers.alpaca.AlpacaClient` history boundary and does not add a second
HTTP client or a listings adapter.

The probe asks for a single bounded history sample per symbol. A response with
no rows is recorded as `empty`, not as proof that the symbol never existed.
Provider and protocol failures are recorded using safe error codes only. The
report never serializes response rows, request headers, exception messages,
credentials, or a run timestamp.

## Reproducible Inputs

The default sweep inputs are fixed and are also written into every JSON report:

| Input | Value |
| --- | --- |
| Data type | `trades` |
| Start | `2010-01-01T00:00:00Z` |
| End | `2025-01-01T00:00:00Z` |
| Feed | `sip` |
| Adjustment | `raw` |
| Provider page limit | `100` |
| Maximum records per symbol | `1` |
| Maximum symbols | `30` |
| Request ordering | normalized, de-duplicated, lexicographic |

The command line requires `--live` before it constructs a settings-backed
Alpaca client. Symbols can instead be supplied with `--symbols` or a
`--symbols-file`; custom inputs retain the same bounds and safe serialization.
For example:

```text
python -m lake.tools.delisted_probe --live --output coverage.json
```

Runtime API credentials must be supplied through the existing environment or
secret provider. They are not command-line arguments and are never included in
the report.

## Per-Symbol Results

The bounded live run on 2026-09-02 used the inputs above and wrote the
credential-free report to
`.github/research/alpaca-delisted-coverage.json`. It returned 22 symbols with
records, 8 empty responses, and 0 errors. An empty response is recorded as
`empty`, not as proof that a symbol never existed. The `cohort` and event year
identify why each symbol is in the representative set.

| Symbol | Cohort | Event year | Result |
| --- | --- | ---: | --- |
| `AGN` | acquisition | 2020 | records |
| `ALXN` | acquisition | 2021 | records |
| `ATVI` | acquisition | 2023 | records |
| `BCR` | acquisition | 2017 | records |
| `CELG` | acquisition | 2019 | records |
| `CTXS` | acquisition | 2022 | records |
| `ESRX` | acquisition | 2018 | records |
| `MON` | acquisition | 2018 | records |
| `MYL` | acquisition | 2020 | records |
| `NBL` | acquisition | 2020 | records |
| `RHT` | acquisition | 2019 | records |
| `SPLK` | acquisition | 2024 | records |
| `TIF` | acquisition | 2021 | records |
| `TWTR` | acquisition | 2022 | records |
| `WORK` | acquisition | 2021 | records |
| `BBBYQ` | bankruptcy | 2023 | records |
| `CHKAQ` | bankruptcy | 2017 | empty |
| `DNDNQ` | bankruptcy | 2016 | empty |
| `ENRNQ` | bankruptcy | 2001 | empty |
| `FTR` | bankruptcy | 2020 | records |
| `HTZ` | bankruptcy | 2020 | records |
| `JCPNQ` | bankruptcy | 2020 | empty |
| `LEHMQ` | bankruptcy | 2008 | empty |
| `SIVBQ` | bankruptcy | 2023 | records |
| `WAMUQ` | bankruptcy | 2008 | empty |
| `WCOMQ` | bankruptcy | 2002 | empty |
| `YRCWQ` | bankruptcy | 2023 | empty |
| `CHL` | delisted | 2021 | records |
| `GM` | reused | 2009 | records |
| `META` | reused | 2022 | records |

## Offline Evidence

The owned tests use both injection points requested for this sweep:

- A fake provider returns records, an empty response, and an HTTP error. The
  test verifies one call per normalized symbol, `max_records=1`, safe error
  codes, and the supplemental-data recommendation.
- A fake transport is injected into the existing `AlpacaClient`. The test
  verifies the stock history path, SIP/raw parameters, the fixed limits, and
  the absence of credential headers.
- Serialization is checked twice from the same report and is byte-for-byte
  identical. It contains no `generated_at` field.

These are offline behavior results only. They demonstrate request construction,
normalization, bounds, serialization, and safety; they do not establish what
Alpaca returns for symbols outside the bounded run above.

## Live Bounded Evidence

The live run used `--live`, read credentials from process environment variables,
requested one trade record per symbol, and never persisted credentials or
provider response bodies. The report records only normalized inputs, safe
statuses, counts, and UTC timestamps for returned records. It is suitable for
review against a supplemental listings source, but it does not establish
listing identity or lifecycle dates by itself.

## Recommendation

**GO, provisionally: retain/add supplemental listings data.** The existing
Alpaca history boundary can answer whether a bounded market-data query returned
rows, but it cannot establish listing identity, lifecycle dates, ticker reuse,
or whether an empty response reflects provider retention or an invalid symbol.
The live sweep demonstrated both retained history and empty responses across the
representative cohorts. Supplemental listings data remains the safer source for
symbol lifecycle and identity joins because Alpaca history alone cannot explain
whether an empty response reflects retention, an invalid symbol, or a lifecycle
event.

The remaining review step is to compare each `empty` result and the historical
dates for each `records` result against the listings source before deciding
whether the supplemental source can be narrowed or removed.
