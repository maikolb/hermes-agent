# WhatsApp passive intake

Passive intake turns explicitly registered WhatsApp groups into receive-only
sources. A matching message is consumed in the Node transport before Hermes'
ordinary allowlist, Python queue, session, memory, tools, or conversational
model. DMs and groups not registered here retain their existing behavior.

## Safe default

The feature is disabled unless `enabled` is exactly `true` and at least one
valid route is present under the WhatsApp platform's `extra` configuration:

```yaml
gateway:
  platforms:
    whatsapp:
      extra:
        passive_intake:
          enabled: false
          routes: []
```

Each enabled route must contain only:

- `project`: a unique lowercase slug containing letters, digits, or hyphens;
- `jid`: the unique, exact WhatsApp group JID ending in `@g.us`.

Configuration is strict. Invalid JSON shape, duplicate projects, duplicate
JIDs, non-group destinations, unsupported fields, or an enabled empty route
set stop the bridge before it connects. Route changes alter the health
fingerprint so the gateway does not silently reuse a bridge with stale rules.

## Storage and isolation

The adapter fixes the spool root to the active Hermes profile at
`platforms/whatsapp/passive-intake`. Every project receives separate `private`
key material and a separate date-partitioned `spool`. Filenames are content
hashes; phone numbers and group JIDs are not used in paths or logs. Envelope
contents are AES-256-GCM encrypted at rest, and reporter identities are
pseudonymized with a project-specific key.

Replays publish with create-if-absent semantics and do not create a second
record. A persistence error is fail-closed: the event remains consumed and is
never allowed to fall through to Titan.

The first milestone records bounded text, captions, reply IDs, and attachment
metadata. Attachment bytes remain marked `mediaCaptured: false` until the
isolated media worker is enabled in the next milestone.

## Receive-only guarantee

Registered route JIDs are denied by both the shared `sendWithTimeout` boundary
and the bridge handlers for send, edit, media, poll, location, typing, read
receipt, and group metadata operations. The denial returns
`PASSIVE_INTAKE_EGRESS_DENIED` and never includes the protected JID.

Do not activate routes until the exact JIDs have been obtained without sending
test traffic and the shadow spool has been inspected. Activation and a gateway
restart are operational changes outside this implementation milestone.
