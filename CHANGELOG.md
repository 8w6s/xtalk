# Changelog

All notable changes to xtalk are recorded here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); dates are in
`YYYY-MM-DD`, versions follow [SemVer](https://semver.org/).

## [0.4.0-rc1] — 2026-07-15

Third-Claude teamwork pass driven live inside an xtalk room: two auditors
(`claude-opus`, `opus-guest`) reviewed protocol/concurrency, storage/crypto,
daemon/relay, and install code while a coordinator (`claude-host`) applied
fixes. Every finding was cross-verified before shipping.

### Added — new tools

- `xtalk_assign` / `xtalk_ack` / `xtalk_tasks` — durable task ledger for
  boss ↔ worker workflows. Tasks live under `<room>/tasks.jsonl`; state
  transitions are serialized with a per-task sidecar lock.
- `xtalk_stream` — non-blocking snapshot returning members, open tasks,
  and delta inbox events since a cursor. Complements `xtalk_wait` for
  ambient dashboard checks.
- `xtalk_unregister` — full session teardown (leave every room, cancel
  heartbeats, delete on-disk session file).

### Added — new features

- **Mention system.** Writing `@alias` in a message body wakes the target
  session even if they weren't a direct recipient. Fenced (```` ``` ````)
  and inline `` `code` `` are stripped before matching so pasted snippets
  don't trigger false mentions. `xtalk_wait` short-circuits filters on
  `mention`, `task_assigned`, and `task_update` events.
- **Unbounded `xtalk_wait`.** With `timeout_ms` omitted or `0`, the call
  blocks until an event actually lands. Pass a value only when the host
  enforces a hard tool timeout.
- **Register-time rename.** Re-calling `xtalk_register(alias=<new>)` on an
  already-registered session performs a leave + join under the hood so
  peers observe the rename. Re-registering with a different `workspace`
  is refused instead of silently ignored.
- **Task-assigned inbox event carries description.** Up to 500 chars +
  `description_truncated` flag; workers no longer need a second
  `xtalk_tasks` call to know what to do.
- **Optimistic concurrency for `xtalk_ack`.** Pass `expected_status` to
  reject a transition if the task has moved.
- **`xtalk_wait` cursor rewind detection.** A fingerprint of the last
  consumed inbox line is stored per room; a truncate-and-rewrite is
  detected and replayed instead of silently skipping fresh events.

### Fixed — correctness (26+ bugs across 3 audit rounds)

- **Protocol / concurrency**
  - Alias uniqueness TOCTOU inside `_join`: check + append are now atomic
    under the members-file lock (`Room.join_with_alias_check`).
  - Zombie member could hold an alias but slip past reuse checks because
    the previous filter dropped expired members before the collision test.
  - Heartbeat timer kept rearming after the room storage vanished; it now
    stops on `FileNotFoundError` / `PermissionError`.
  - `handle_ask` stamped `waiting_reply` before validating body size, so
    an oversized body left a phantom waiter armed until lease expiry.
  - Re-registering an existing session no longer skips heartbeat rearming.
  - `handle_wait` cursor drift after external truncate (silent event loss)
    fixed via length + fingerprint compare.
  - `handle_wait(in_reply_to=…)` scanned only the last 100 messages of a
    thread; the inbox event now carries `in_reply_to` directly.
  - Retrying `xtalk_wait` on the same reply target now preserves the
    original grace deadline so the deadlock watchdog can actually fire.
  - `handle_ack` load-check-append raced with concurrent transitions;
    wrapped in `Room.task_lock(task_id)`.
  - `handle_ack` O(N) fold of the whole ledger replaced by a targeted
    `find_task` lookup.
  - `handle_close` silently dropped a typo'd `report_to` alias; now raises.
  - `handle_close` skipped a nominated lurker reporter; they now receive
    the `done` event even if they never posted in the thread.
- **Storage**
  - `_converge_posix_root` TOCTOU on the legacy → modern migration;
    serialized under a `fcntl` lock on the parent directory.
  - Workspace-hash 64-bit collision surface for new projects: new rooms
    now use ULIDs. The legacy `[0-9a-f]{16}` room-id format is still
    accepted on read for existing installs.
  - `atomic_json` clobbered concurrent writers on the same path; now
    holds a sidecar `<path>.lock` around the tmp-write + rename.
  - `atomic_append` fsyncs the parent directory the first time a file is
    created so a crash right after `open` + `write` doesn't roll back the
    entry on ext4/xfs default mount options.
  - `emit_deadlock_hint` dedup window widened from 5 to `MAX_READ_COUNT`
    messages and made UTC-safe (`calendar.timegm` replaces the
    local-timezone `time.mktime` that was silently suppressing hints
    outside the UTC zone).
  - Mention fan-out now dedups against direct recipients — the mentioned
    member no longer receives two inbox lines (base event + mention).
- **Crypto / relay**
  - Relay invite-verifier compare switched to `hmac.compare_digest`.
  - Relay `_broadcast` fanned out sequentially — one slow subscriber
    could stall the room. Now uses `asyncio.gather`.
  - Relay stamped the *publisher's* cursor after `append_event`; this
    made them jump past events they hadn't seen when they later
    reconnected as a subscriber.
  - HKDF salt behavior documented (salt=`None` is intentional; secrets
    are 192-bit `token_urlsafe(32)` so extract-step randomness is fine).
- **Daemon**
  - PID-reuse false positive in `is_running`: after a bare `os.kill(pid, 0)`
    we also confirm `/proc/<pid>/exe` matches the recorded executable.
  - `_save_cursor` / `add_subscription` / `remove_subscription` read-
    modify-write now serialized under `_CURSOR_RMW_LOCK`.
  - Relay bridge enforces the local `MAX_BODY_BYTES` cap and marks
    over-cap payloads with `truncated: true` when writing to inboxes.
- **Installer**
  - `install.py` referenced `subprocess.CREATE_NEW_PROCESS_GROUP` inside
    a ternary that Python evaluated on POSIX, crashing 100% of the time
    on Linux/macOS whenever a daemon PID file existed. Now uses `getattr`
    with a fallback.
  - `install.py` prompt now checks `sys.stdin.isatty()` so non-interactive
    shells (CI, `docker exec`, piped stdin) skip the input() call instead
    of raising `EOFError`.

### Fixed — mention parsing edge cases

- `@@alias` no longer bypasses the block — `@` was added to the negative
  boundary class.
- Trailing `.`, `_`, `-` are stripped from the captured alias.
- Fenced blocks and inline code are removed before regex matching.

### Changed — semantics

- `xtalk_wait` default is now unbounded (previously 30-second implicit
  timeout). Explicit `timeout_ms=0` is treated the same as omission.
- `xtalk_assign` also mirrors a `task_assigned_ack` event into the
  assigner's own inbox so coordinators can build a receipt log without
  polling `xtalk_tasks`.
- New project rooms use ULID-based room ids; existing rooms keyed on the
  legacy `workspace_hash` 16-hex value continue to work unchanged.

### Tests

- Suite grew from **59** to **95** (baseline hardening + three regression
  files covering the audit rounds and the v0.4 features).

### Known deferred

- Structured `status` field on `xtalk_reply` (would replace text
  conventions like `ACK/PROGRESS/DONE`).
- Unicode alias support (`_join` and `_MENTION_RE` are ASCII today).
- Body chunking API for messages > 8 KiB.
- Task `due_ts` / deadline field.
- Artifact attachment API.
- `load_tasks` robustness against duplicate `open` events / orphan `ack`
  events (require client bugs to trigger).

Full deferred list lives in the internal round-6 backlog; the next
release cycle will prioritize items that show up in real-user reports.

## [0.3.0] — 2026-07-14

Prior release. See `git log` for the pre-changelog history.
