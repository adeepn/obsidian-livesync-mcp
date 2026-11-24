# Local state and recovery

The state directory is a **rebuildable replica**, not an eviction cache.
`journal.sqlite3` holds revision trees, replay checkpoints and peer metadata;
SQLite WAL/SHM sidecars may also be present. `chunks/` holds content-addressed
chunks, including historical chunks. State grows with vault content and history
and is **never auto-pruned** during normal operation. Remote journals are
append-only, so deleting a note does not reclaim its history here.

Keep a dedicated writable `S3_STATE_DIR` per vault/peer, persisted across restarts.
Allow roughly vault-sized disk space initially and potentially much more for
history, revision metadata and SQLite overhead. Replay time depends on journal
volume and network speed; there is no fixed completion time. Large histories
may need multiple refresh calls because replay has per-call budgets.

Monitor `obsidian_vault_stats` → `local_state`: `path`, `bytes`,
`human_readable_size`, `warn_threshold_mb`, and `warning`. Size is a best-effort
sum of logical SQLite/sidecar and chunk-file bytes, measured once per call;
it may differ from filesystem allocated space reported by `du`.
Set `S3_STATE_SIZE_WARN_MB=1024`, for example, to warn above 1024 MiB
(1 MB here means 1,048,576 bytes). Zero disables warnings; equality does not
warn. A warning adds a human-readable `hint` to the tool response. This is
observability only: it does not stop writes or delete anything.

For a manual check in a shell with `S3_STATE_DIR` set to the configured path:

```bash
du -sh "$S3_STATE_DIR"
```

`.env` is loaded by `run.py`, not automatically by your shell. If the setting
is unset, check the default `./data` relative to the server's working directory.

To reset safely:

1. Stop the server (for systemd: `sudo systemctl stop obsidian-mcp`) and any
   other process using that state directory. Confirm the remote vault is intact.
2. Check the exact configured path. Move the whole state directory aside for
   recovery; keep it until every locally retained note has been accounted for.
   Remote history may no longer contain the only copy of a local survivor.
3. Start the server (`sudo systemctl start obsidian-mcp` for systemd). The next
   tool call replays remote journals to rebuild local state; allow time and disk
   for a full replay. Check statistics and known notes before discarding a backup.

Automatic recovery mirrors the device's checkpoint handling, not database
removal. A missing newest confirmed outbound journal clears replication tracking
(processed journals, outbound records and last-sent); replay restarts from the
beginning. Legacy state without outbound records uses its last-sent key. Local revision trees,
chunk files, and notes absent from the rebuilt remote world remain readable.
Replay is idempotent and does not duplicate revision leaves. A fresh peer sees
only current remote history; retained local survivors are not automatically
republished. Epoch changes (`protocolVersion:pbkdf2salt`) without a missing
last-sent journal merely update the epoch and preserve dedupe caches, as in
pinned `JournalSyncCore.ts:354–400` (including devices with no send history).
Before appending, changed epoch or a missing newest confirmed journal causes `ConcurrentModification`
without changing the local store or writing remote objects; refresh and retry.

Changing path identity quarantines the **whole** old state directory at
`<state_dir>.quarantine/<UTC timestamp and unique suffix>/state` and opens a fresh
store at the configured path. A WARNING names the archive and restoration steps.
No archive is deleted automatically. To restore, stop all processes using this
state, move the current directory aside, move the archived `state` back to the
configured path, and restore the old path-identity settings before starting.
Do not open the same state directory concurrently from multiple processes.

**Superseded (P-14, Task 5 fix round 2):** the previous policy of clearing all
local tables/chunks on epoch/wipe, and refusing identity changes without a
recovery archive, no longer applies. The earlier full-reset description is
retained as historical context in the implementation plan.
