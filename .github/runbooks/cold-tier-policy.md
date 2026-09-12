# Cold-Tier Policy

This runbook records the market-lake lifecycle-tier decision for lake block
blobs. It exists so that the choice to keep Cold disabled is an explicit,
retrieval-cost decision rather than an assumed automatic transition.

## Decision

Cold tier remains **disabled**. Lake block blobs move from Hot to Cool after 14
days without modification and stop there. No object is moved to Cold by
lifecycle policy.

## Rationale

Cold storage lowers the per-GB storage rate but charges for data access. The
retrieval cost, not the storage rate, decides whether the transition pays off:

- Cold reads bill per GB retrieved; a single full-corpus re-read was projected
  at roughly 430 USD in the equity cost model.
- Deleting or overwriting a Cold object before its minimum age adds an
  early-deletion charge.
- The ingest cap and retention rules already bound growth, so the storage-rate
  saving from Cold does not offset the retrieval exposure.

The projection and its inputs are recorded in `docs/e6.4-cost-projection.md`.
The decision uses list-price retrieval cost, not measured access-pattern
telemetry: no per-tier access dataset exists yet.

## Current Policy Surfaces

| Surface | Value | Location |
| --- | --- | --- |
| Hot to Cool | 14 days after modification | `infra/modules/storage.bicep` (`coolAfterDays`), `config/market-lake/settings.yaml` (`lifecycle.cool_after_days`) |
| Cold | disabled | `infra/main.bicep` (`enableColdTier: false`), `config/market-lake/settings.yaml` (`lifecycle.cold.enabled`) |
| Cold age | 90 days (inactive) | `infra/modules/storage.bicep` (`coldAfterDays`) |

While Cold is disabled the `coldAfterDays` (90) and
`lifecycle.cold.minimum_age_days` (365) values are inert and need not agree.

## Re-Evaluation Trigger

Re-open this decision when either condition holds:

- per-tier access telemetry shows a stable read pattern old enough to make Cold
  retrieval predictable; or
- the ingest cap or retention policy changes enough that the Cool-tier storage
  rate becomes the dominant cost.

## Changing The Decision

1. Set `enableColdTier: true` and choose `coldAfterDays` in `infra/main.bicep`.
2. Align `lifecycle.cold.minimum_age_days` in `config/market-lake/settings.yaml`.
3. Update this runbook and the deployment note in
   `.github/architecture/repository.md`.
4. Deploy and confirm the management policy with
   `az storage account management-policy show`.

## Verification

- `infra/modules/storage.bicep` keeps the Cold action conditional on
  `enableColdTier`.
- `uv run --extra dev pytest -p no:cacheprovider tests/lake -q` and the Bicep
  gates pass.
