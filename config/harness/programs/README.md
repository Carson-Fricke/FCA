# Authored research programs

`msft_continuation.rs` is the corrected MSFT continuation research implementation.
It uses SDK fill accounting, tracks the full requested order across partial fills,
exits actual inventory, and closes an episode only after the exit is fully filled
and the position is flat. Custom metrics expose actual fees, open quantity and
pending orders. Historical frozen execution inputs remain immutable.
