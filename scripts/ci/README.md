# Release verification

- `installed_sdk_smoke.py` exercises public program templates, SDK identity, and
  reference reads from an installed wheel with Python isolated from the checkout.
- `wait_aca_revision.sh` waits for the actual latest Azure Container App revision,
  validates its image digest and ready containers, and requires API probe health.
  It never accepts a previously healthy revision as evidence of a new rollout.

Build and deployment checks belong here, outside the pytest behavior suite.
