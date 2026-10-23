# supervision

- `clock.rs`: Owns trader supervision clock; lifecycle coordination remains in the parent.
- `configuration.rs`: Owns trader supervision configuration; lifecycle coordination remains in the parent.
- `feed.rs`: Owns trader supervision feed; lifecycle coordination remains in the parent.
- `journal.rs`: Owns trader supervision journal; lifecycle coordination remains in the parent.
- `mod.rs`: Owns trader lifecycle decisions over explicit clock, feed, snapshot and journal ports.
- `models.rs`: Owns trader supervision models; lifecycle coordination remains in the parent.
- `process.rs`: Owns trader supervision process; lifecycle coordination remains in the parent.
- `snapshots.rs`: Owns trader supervision snapshots; lifecycle coordination remains in the parent.
