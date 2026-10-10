# CLI behavior tests

`api_composition.rs` verifies that published API startup validates configured
Azure storage and that protected HTTP routes reject anonymous callers. Tests
use the CLI composition boundary without contacting Azure or Alpaca.
