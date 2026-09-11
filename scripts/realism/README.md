# Realism prototypes

Deterministic, offline prototypes backing the post-v1 realism spikes in
`docs/post-v1-realism-spikes.md`. Each script uses only embedded data and exact
`Decimal` arithmetic; none reads the wall clock, the network, or unseeded
randomness.

| Script | Spike | Purpose |
| --- | --- | --- |
| `corporate_actions_adjusted_vs_unadjusted.py` | S1 | Show the unadjusted-versus-adjusted divergence on the known 2020-08-31 Apple 4-for-1 split. |

Run a prototype:

```powershell
uv run --no-sync python scripts/realism/corporate_actions_adjusted_vs_unadjusted.py
```

The same files are focused pytest modules when passed explicitly:

```powershell
uv run --no-sync pytest scripts/realism/corporate_actions_adjusted_vs_unadjusted.py
```

Keep prototypes deterministic. Do not add wall-clock, network, subprocess, or
unseeded-random behavior; if a model needs variation, seed it explicitly.
