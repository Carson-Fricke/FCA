# Azure Table HTTP behavior

`azure_http.rs` verifies that independent upserts overlap and retain separate success/failure results through the real Azure SDK. Only HTTP is simulated. The source module includes this file by path so production construction needs no test-only interface.
