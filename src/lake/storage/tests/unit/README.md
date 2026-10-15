# Azure HTTP behavior

`azure_http.rs` exercises conditional lock creation, empty-lock reuse, competing lease ownership, permission failures, ordinary object replacement, and overlapping independent uploads with isolated results through the real Azure SDK. Only HTTP is simulated. The path include in `src/azure.rs` permits private client construction; no test lives inline in production source.
