# Azure HTTP behavior

`azure_http.rs` exercises conditional lock creation, empty-lock reuse, competing lease ownership, permission failures, object replacement, overlapping uploads and bounded metadata reads through the real Azure SDK. Hierarchical listings exclude directories while retaining normal and zero-byte files. Only HTTP is simulated. The path include in `src/azure.rs` permits private client construction; no test lives inline in production source.
