# thesis_runtime/contracts

Neutral value and transport contracts shared by runtime components and adapters. Domain references remain opaque; native provider roles are not session types.

- `__init__.py`: contracts capability package.
- `activity.py`: Tagged runtime activity metadata; untagged records are rejected.
- `codec.py`: Encodes neutral durable records and validates their complete current fields with shared typed adapters.
- `context.py`: Trusted tool context attached by the runtime to every dispatch.
- `datum.py`: Typed datums with required rendering types: the contract between tool results and the chat surface.
- `errors.py`: Transport-neutral canonical delivery, provider, and extension catalog failures, including explicit transient-provider classification and optional retry timing. Recovery eligibility never declares an unknown attempt safe to replay.
- `messages.py`: Provider-neutral session values and the durable transcript decode boundary.
- `records.py`: Workspace-scoped contracts for Thesis chat and autonomous work.
- `output.py`: Provider-neutral reasoning and answer fragments, separate from the session inbox.
- `reply.py`: Neutral tool-result observation contracts.
