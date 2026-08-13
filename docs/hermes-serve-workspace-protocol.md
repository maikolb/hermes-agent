# Hermes Serve Workspace Protocol v1

This document defines the stable JSON-RPC 2.0 contract exposed by
`hermes serve` over its existing `/api/ws` WebSocket. It is an additive layer
over the dashboard/TUI protocol: existing methods and the `session.create`
response remain compatible.

## Transport and envelopes

Clients send JSON-RPC requests as WebSocket text frames. Responses use the
usual `{ "jsonrpc": "2.0", "id", "result" }` or `error` shape. Server events
are JSON-RPC notifications whose `params` contain:

```json
{
  "type": "message.delta",
  "event_id": "a1b2c3d4:7",
  "event": "message.delta",
  "session_id": "a1b2c3d4",
  "turn_id": "portal-turn-42",
  "sequence": 7,
  "occurred_at": "2026-08-12T22:40:10.123456Z",
  "payload": {"text": "hello"}
}
```

`type` is the legacy dashboard event name and is retained for old clients.
New clients consume `event`. Sequence numbers are monotonically increasing
within one live session and are suitable for ordering/reconciliation during a
connection lifetime. A resumed durable session creates a new live session and
therefore a new event sequence.

Normalized event families are `session.ready`, `session.closed`,
`turn.started`, `turn.redirected`, `turn.cancelled`, `turn.completed`,
`turn.failed`, `reasoning.delta`, `message.delta`, `message.completed`,
`tool.started`, `tool.completed`, `agent.status`, and `todo.snapshot`.

`workspace.capabilities {}` returns `contract: "hermes.workspace"`, the
`contract_version`, and the complete stable method/event surface. Portal
clients must require major version `1` before dispatching turns.

## Sessions

`session.create` is unchanged. In addition to its existing response it emits
`session.ready` after the live session has been registered.

`session.resume` accepts:

```json
{
  "session_id": "durable-session-id",
  "profile": "optional-profile",
  "cwd": "/optional/client/hint",
  "source": "workspace-portal"
}
```

The existing resume behavior remains authoritative for the persisted working
directory. The additive result fields are `stored_session_id` and
`state: "ready"`; `session_id` remains the live ID used by all turn methods.

## Turns

### `turn.start`

Parameters:

```json
{
  "session_id": "live-session-id",
  "turn_id": "caller-generated-opaque-id",
  "text": "implement the next task",
  "attachments": [],
  "idempotency_key": "message-or-run-id"
}
```

Exactly one active turn is allowed per live session. Repeating the same
`idempotency_key` with the same payload returns the original acknowledgement
with `replayed: true` and never submits a second prompt. Reusing the key with a
different payload is rejected. Supported attachment entries are a local image
path (`string` or `{path}`) and the existing `image.attach_bytes` data shape;
the portal must stage opaque uploads before invoking Hermes.

### `turn.redirect`

Parameters:

```json
{
  "session_id": "live-session-id",
  "turn_id": "active-turn-id",
  "message_id": "portal-message-id",
  "text": "use the other API instead",
  "sequence": 18
}
```

`message_id` is the idempotency identity. New redirect sequences must be
strictly increasing. Accepted redirects are appended to the turn's ordered
buffer; no redirect overwrites an earlier one. If the active model response
accepts the correction, the disposition is `redirected`. If the response ends
during the call, Hermes delegates the text to the existing atomic prompt claim
path and returns `queued_after_race` plus a deterministic `next_turn_id`.

### `turn.cancel`

`{session_id, turn_id, reason?}` cooperatively interrupts only the requested
active turn. The acknowledgement status is `cancelling`; terminal confirmation
arrives as `turn.cancelled`. Repeating cancellation after a terminal state is a
read-only acknowledgement.

### `turn.info`

`{session_id, turn_id?}` is read-only. With `turn_id`, it returns that turn and
whether it is active or queued. Without it, it returns the current turn,
ordered redirects and an optional queued successor. This method is the recovery
probe after a portal or WebSocket restart.

## Compatibility and ownership

The adapter delegates to `prompt.submit`, `session.redirect`,
`session.interrupt`, and the existing session registry. It does not import the
portal, read a provider directly, or make the WebSocket transport the owner of
conversation state. Dashboard/TUI clients may continue using their existing
method names and event `type` values.
