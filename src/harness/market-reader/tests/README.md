# Reader behavior

- `snapshots.rs` verifies canonical manifest identity, concurrent buffer reuse, disk reuse across reader restarts, rejection of corrupt or modified content, overlapping partition downloads with ordered delivery, deferral of oversized lookahead objects until their required head position, and cancellation/reuse after visitor failure. Only the external byte source is fake.
