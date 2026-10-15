# Reader behavior

- `snapshots.rs` verifies canonical manifest identity, concurrent buffer reuse, disk reuse across reader restarts and rejection of corrupt or modified content. Only the external byte source is fake.
