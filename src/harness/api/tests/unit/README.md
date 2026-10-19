# Pure protocol checks

Public-boundary execution behavior belongs to the parent test folder.

- `execution_launch.rs`: validates the current Azure name/resource-ID acknowledgement shapes and rejects conflicting or unscoped identities before attribution reads. This is pure protocol parsing; it performs no network or process work.
