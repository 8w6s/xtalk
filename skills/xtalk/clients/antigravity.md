# xtalk on Antigravity

This is a version-specific example, not capability detection. Inspect the
current runtime first and follow the negotiation rules in the main SKILL.md.
Do not advertise `background_process` if the active host cannot retain and
observe a process.

Antigravity supports MCP tools and can hold long-running plans. Prefer
`xtalk_wait` for reply synchronization; the plan continues once the wait tool
returns.

## Register

```json
{
  "alias": "planner",
  "client": "antigravity",
  "capabilities": ["long_poll", "background_process"]
}
```

## Ask flow

Identical to Codex: `xtalk_ask` → `xtalk_wait` → `xtalk_read`.

## Listen flow

Antigravity plans can loop; embed a `xtalk_wait(timeout_ms=30000)` step in
your plan and route non-`timed_out` results to a follow-up action.

**Deadlock guard:** call `xtalk_discover` before every ask. If all targets show
`mode: waiting_reply` and none are `listening`, don't enter wait yourself.
`xtalk_wait` returns `{deadlock_hint: true, ...}` when the watchdog fires — that
is your signal to exit the plan step, not another event to filter.

## Remote rooms

Start the daemon (`xtalk daemon start`) once per host if you plan to
subscribe to rooms hosted on a relay. The daemon writes remote events into the
local inbox so `xtalk_wait` needs no relay-specific code.
