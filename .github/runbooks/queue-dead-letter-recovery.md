# Queue Dead-Letter Recovery

This runbook covers the market-lake queue worker and its dead-letter queue.
It is an operator-triggered recovery path, not an automatic retry loop.

## Delivery Behavior

The Rust lake queue worker (`lake-queue` crate) receives one leased message at
a time. A handler failure below `max_delivery_attempts` leaves the source
message undeleted so Azure Queue visibility timeout can redeliver it. On the
final failed delivery, the worker writes a versioned envelope to the
configured dead-letter queue and deletes the source message only after the
send succeeds. If dead-letter delivery fails, the source message is not
deleted and can be retried safely.

Messages received after the configured attempt limit are dead-lettered without
calling the handler. This prevents a poison message from retrying indefinitely.

## Diagnose

Inspect one dead-letter envelope before replaying it. The envelope has these
fields:

- `dead_letter_id`: stable SHA-256 identity for the source queue, message ID,
  and original content. It remains useful if transfer is repeated after a
  worker crash.
- `source_queue`, `source_message_id`, `source_pop_receipt`: source delivery
  evidence. The pop receipt is diagnostic only and must not be reused for
  replay.
- `dequeue_count`: delivery count observed when the message was transferred.
- `original_content`: the exact source message body.
- `error`, `error_type`, and `error_message`: the worker failure text and its
  parsed type/detail when the handler supplied the standard exception format.
- `failed_at` and `schema_version`: UTC event time and envelope contract.

Check `error` and `original_content` first, then correlate
`source_message_id` and `dead_letter_id` with worker logs. Do not paste
credentials, tokens, or complete sensitive payloads into incident tickets.

## Replay Procedure

1. Confirm the provider, schema, credentials, or deployment issue identified by
   `error` is fixed before replaying.
2. Read and validate the envelope. Confirm `schema_version` is supported and
   `source_queue` is the intended target. Never replay using
   `source_pop_receipt`.
3. For a valid JSON object, use the lake queue replayer (`lake_queue::DeadLetterReplayer`
   in the Rust `lake-queue` crate) with a `QueueSender` for the original
   queue. The replayer sends only the original payload, not the dead-letter
   metadata wrapper, so the normal worker handler receives its usual message
   contract.
4. For malformed or non-object `original_content`, correct the payload through
   the approved change/review path and pass the repaired JSON object explicitly
   as `repaired_payload`. Keep the original envelope unchanged for audit.
5. Replay one message at a time, record the `dead_letter_id`, operator, reason,
   and `ReplayResult`, then monitor the source worker. Do not bulk replay while
   the underlying failure is unresolved.
6. Leave the dead-letter record retained until the replay has been observed as
   successful and the incident record has captured the outcome. If the replay
   fails again, diagnose the new worker failure rather than automatically
   replaying it.

The replayer is intentionally explicit and does not delete dead-letter
records, mark them successful, or retry on its own. A deployment should add
those audit and retention operations around this boundary using its durable
queue tooling. Replaying a message is at-least-once: only replay after the
handler's idempotency contract and catalog/object replacement behavior are
verified.

## Safety Limits

- Use the configured `max_delivery_attempts`; do not raise it to mask a
  permanent failure.
- Dead-letter transfer and source deletion are separate operations. The stable
  envelope ID makes duplicate transfers recognizable, but cannot make two
  independent Azure queue operations atomic.
- This repository implementation uses injected `QueueSender` and store fakes
  in tests. No live Azure queue, provider, credential, or network access is
  required for validation.
