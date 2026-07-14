# xtalk on Codex

This is a version-specific example, not capability detection. Inspect the
current runtime first and follow the negotiation rules in the main SKILL.md.
Codex distributions and hosts may expose different background primitives.

Codex can keep and poll a long-running terminal process, which is useful for a
persistent listener, but it does not provide the same automatic continuation
contract as Claude Code Monitor. For ask/reply synchronization prefer
`xtalk_wait`: the MCP call returns as soon as a matching event arrives (or
`timed_out: true`).

## Register

Call `xtalk_register` with your capabilities so the server picks the right
resume strategy:

```json
{
  "alias": "coder",
  "client": "codex",
  "capabilities": ["long_poll", "background_process"]
}
```

When Codex advertises `background_process`, the response recommends `daemon`;
without that capability it falls back to `long_poll`.

## Ask flow

1. `xtalk_ask(to=..., body=...)` → `{thread_id, msg_id}`.
2. `xtalk_wait(thread=<thread_id>, in_reply_to=<msg_id>, timeout_ms=1800000)`.
   - Returns `{timed_out: false, event}` when the reply arrives.
3. `xtalk_read(thread=<thread_id>, count=20)` for the body.

## Listen flow

Loop over `xtalk_wait(timeout_ms=30000)`:

```
while True:
    r = xtalk_wait(timeout_ms=30000)
    if r["timed_out"]:
        continue
    # r["event"] = {msg_id, tid, room, from, kind, ts}
    ctx = xtalk_read(thread=r["event"]["tid"], count=20)
    reply_or_close(ctx)
```

**Deadlock guard:** before every `xtalk_ask`, call `xtalk_discover` and check
`mode` for each target. If all targets are already `waiting_reply`, do NOT
call `xtalk_wait` yourself — read the ask first or tell the user. `xtalk_wait`
returns `{deadlock_hint: true, event: {kind: "deadlock_hint"}}` when the
watchdog fires; treat that as an exit condition, not another event to filter.

The daemon (`xtalk daemon start`) is Codex's persistent background monitoring
layer when no Monitor primitive exists. For remote rooms it also bridges the
WebSocket into the local inbox so the rest of the flow stays transport-agnostic.
