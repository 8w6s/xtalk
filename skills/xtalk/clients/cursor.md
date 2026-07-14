# xtalk on Cursor

This is a version-specific example, not capability detection. Inspect the
current runtime first and follow the negotiation rules in the main SKILL.md.
Cursor hosts and extensions may add background primitives over time.

Cursor's Composer runs the MCP client per-turn. It does not keep a persistent
background listener while the human is idle. Two patterns work:

## Interactive ask (best for Cursor)

Send a question and check for the reply in the same tool sequence using
`xtalk_wait` with a short timeout (10–30s). If it times out, tell the user and
let them re-run.

```
xtalk_register(alias="cursor-ide", client="cursor", capabilities=["long_poll"])
xtalk_ask(to="reviewer", body="...")
xtalk_wait(thread=<tid>, in_reply_to=<mid>, timeout_ms=30000)
```

Long waits (>30s) are usually a bad UX in Cursor — prefer to fire off the ask,
let the reviewer session take time, and have the user re-invoke Composer to
collect the reply via `xtalk_wait`.

## Listen mode

Cursor cannot sit in a `Monitor`-style loop. If you must respond to incoming
questions, poll on demand:

```
xtalk_wait(timeout_ms=1000)
```

**Deadlock guard:** `xtalk_wait` returns `{deadlock_hint: true, ...}` when the
watchdog spots a mutual-wait deadlock. Exit the poll loop and tell the user
instead of re-entering `xtalk_wait`.

The daemon (`xtalk daemon start`) helps when Cursor works against a remote
relay by keeping the inbox up-to-date between poll calls.
