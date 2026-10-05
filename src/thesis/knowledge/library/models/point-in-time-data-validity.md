---
id: point-in-time-data-validity
title: Point-in-time data validity
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: eb77ba0f55b0ab5c638a6e7f137d00ac08a8ca42d5069e9e0c94bc598c10222b
tags:
  - point-in-time
  - survivorship-bias
  - corporate-actions
  - data-quality
  - bad-ticks
collections:
  - model-cards
  - data-quality
links:
  - model-selection-by-hypothesis-shape
  - cross-sectional-momentum-and-rank-ic
  - structural-breaks-and-regime-tests
  - overfitting-and-selection-discipline
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: cross-sectional-momentum-and-rank-ic
  - type: relates
    target: structural-breaks-and-regime-tests
  - type: relates
    target: overfitting-and-selection-discipline
trust:
  tier: curated
  state: published
---
# Point-in-time data validity

Every other card assumes the data is what it claims to be. This card tests
that assumption: was the universe knowable at the time, were corporate
actions handled, are the prints real, and do sessions line up? Data problems
add signal that does not exist.

## In brief

- What it tests: whether the data reproduces the information set that
  existed at decision time.
- Intake: as-of snapshots or vintages, membership history, action history,
  and tick-level quality checks.
- Key robustness: publish data-validity outputs such as `data_ok` and
  `sample_valid` alongside the result instead of quietly cleaning.
- Main trap: survivorship and restated data make almost any long-short
  claim look better.
- A falsifier that cannot fail on bad data is not a falsifier.

## What it tests

Three validity claims: the universe is as-of, not today's list; prices and
returns are adjusted consistently for splits and dividends; and the
observations are real prints in the right session. A result is tested on
the validity-adjusted sample and compared with the unadjusted one. The gap
between them is the size of the data artefact.

## Required data

Membership and listing history with entry and exit dates, delisting returns,
corporate-action records, symbol-change maps, and session calendars
including half-days and halts. Tick or bar data with a cleaning log. For
intraday studies, timestamps and timezones must be resolved before any other
test. As-of or vintage data is required for any fundamental field.

## Simplest useful test

Re-run the headline result twice: once on the point-in-time universe with
cleaned prices, and once on the naive current universe. Publish the
comparison and set `data_ok` to zero when the validity checks fail. Then run
a small battery: duplicate timestamps, crossed quotes (bid above ask),
impossible bars (high below low), zero or negative prices, missing sessions,
and unexplained gaps.

## False explanations

- **Survivorship.** Delisted losers are absent, flattering long-short
  spreads.
- **Look-ahead membership.** Index or universe membership backfilled from
  today's list.
- **Restated fields.** Fundamentals and signals revised after the fact.
- **Corporate actions.** Unadjusted splits and dividends create jumps that
  look predictive.
- **Bad ticks.** One wrong print can create a fake break or fake vol
  spike.
- **Session handling.** Overnight moves pooled with intraday returns, or
  auction prints mixed with continuous trading.

## Statistical test

Data validity is a sensitivity test, not a significance test. The statistic
is the difference between the clean and naive result, with the same robust
inference as the underlying claim. Report counts: dropped rows, corrected
rows, missing sessions, and unmatched symbols. Publish `data_ok` and
`sample_valid` as outputs with declared thresholds so a later reader can
reject the result. A cleaning rule chosen after seeing results is a
selection degree of freedom.

## Economic test

Bad data manufactures phantom edges, and over-cleaning removes real
observations. Show how costs change when the universe is point-in-time, when
delisting returns are included, and when spreads are measured on cleaned
quotes. A result that only exists on the naive sample has no economic
interpretation, regardless of its cost model.

## Failure conditions

- Observable: `data_ok`; condition: result interpretable; threshold:
  `data_ok < 1.0` when any declared validity check fails.
- Observable: `sample_valid`; condition: enough clean observations;
  threshold: valid fraction below the declared minimum.
- Observable: point-in-time comparison; condition: robust to universe;
  threshold: sign or significance changes between as-of and current.
- Observable: price sanity; condition: usable prints; threshold: any
  crossed quote, impossible bar, or non-positive price unexplained.
- Observable: session coverage; condition: contiguous sample; threshold:
  missing sessions above the declared tolerance.

## Limitations

No dataset is perfectly point-in-time; the question is whether the remaining
bias can change the conclusion. Cleaning rules are researcher choices too.
