# Local state and recovery

The state directory is a **rebuildable replica**, not an eviction cache.
`journal.sqlite3` holds every document a device holds (chunk documents
included), revision trees, replay checkpoints and peer metadata; SQLite WAL/SHM
sidecars may also be present. `chunks/` holds chunk files written by releases
before chunk documents; they are read only for a chunk no document names and
are not written any more. State grows with vault content and history.
The peer prunes nothing on its own and compacts exactly what a device
compacts: a merge that stems a revision away removes its stored body
([peer limits](peer-limitations.md)), so the local state is a working replica,
not a revision archive. Remote journals are append-only and keep every record,
a deleted note's included; a fresh state directory is rebuilt from them.

Keep a dedicated writable `S3_STATE_DIR` per vault/peer, persisted across restarts.
Allow roughly vault-sized disk space initially and potentially much more for
history, revision metadata and SQLite overhead. Replay time depends on journal
volume and network speed; there is no fixed completion time. Large histories
may need multiple refresh calls because replay has per-call budgets.

Monitor `obsidian_vault_stats` → `local_state`: `path`, `bytes`,
`human_readable_size`, `warn_threshold_mb`, and `warning`. `path` is redacted
as `<value hidden>`; use the configured `S3_STATE_DIR` for filesystem commands.
Size is a best-effort
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
store at the configured path. A WARNING reports quarantine and restoration
steps with paths/identity values omitted. Find the archive using the configured
state path and the quarantine pattern above.
No archive is deleted automatically. To restore, stop all processes using this
state, move the current directory aside, move the archived `state` back to the
configured path, and restore the old path-identity settings before starting.
Do not open the same state directory concurrently from multiple processes.

**Superseded (P-14, Task 5 fix round 2):** the previous policy of clearing all
local tables/chunks on epoch/wipe, and refusing identity changes without a
recovery archive, no longer applies. The earlier full-reset description is
retained as historical context in the implementation plan.
