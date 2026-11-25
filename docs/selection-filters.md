# Device file selection

Ignore-file settings derive from vault milestone/PREFERRED tweaks unless each
variable is independently overridden. PREFERRED wins when present; otherwise
all readable device maps must agree on active settings. Missing useIgnoreFiles
defaults to false, and missing ignoreFiles defaults to `.gitignore`. Invalid
active types or disagreeing active maps refuse startup and name the override
to set. Inactive ignore names are never validated. Names are split on commas
and JavaScript-trimmed with empty entries retained; names have no startup
admission gate. Unusable names contribute no rules at bounded storage lookup.
Regex lists stay env-only because syncOnlyRegEx and syncIgnoreRegEx are not
TweakValuesTemplate keys and are not advertised in milestone snapshots.

| Variable | Default | Meaning |
|---|---|---|
| `LIVESYNC_SYNC_ONLY_REGEX` | empty | Require any allow expression to match when the parsed list is nonempty |
| `LIVESYNC_SYNC_IGNORE_REGEX` | empty | Reject when any ignore expression matches |
| `LIVESYNC_USE_IGNORE_FILES` | empty | Unset/empty derives from vault; true/false explicitly overrides |
| `LIVESYNC_IGNORE_FILES` | empty | Unset/empty derives from vault; non-empty explicitly overrides names, e.g. `.gitignore,.syncignore` |

Regex lists use **`|[]|`**, not commas or newlines. ASCII spaces and LF are
removed; quotes are literal, empty items are discarded, duplicates remain.
`!!` inverts an individual expression. Patterns use ECMAScript RegExp search
against the complete path, without implicit anchors, with `i` unless the vault
uses case-sensitive filenames. `/foo/g` is literal regex source, not JS literal
syntax; `g`, `y`, `u`, and `v` flags cannot be configured by these settings.
For example, `\.md$|[]|\.canvas$` allows either suffix.

Selection filters are off when the effective vault/operator policy is off. Without Node.js the server is fully
functional with filters off; it never launches Node in that mode. With any
selection filter active, including vault useIgnoreFiles=true, both backends require **Node.js 22+** in the service
PATH and refuse to start if the executable, version check, or runner self-test
is unusable. The startup error names the cause and how to resolve it.

The peer runs pinned upstream functions and vendored minimatch, rather than
translating to Python regex/glob. One persistent Node worker per backend uses
id-correlated JSON lines over stdio, with one concurrent evaluation admitted
before dispatch to a thread. Active-filter startup is its first use; otherwise
it starts lazily. Each new process checks the Node version and runs a health
self-test. It restarts on the next request after a crash; a failed request is
never silently retried. Backend close terminates and reaps the process and
joins its pipe threads. Five-second evaluation deadlines include blocked stdin;
timeout, nonzero exit, malformed responses and invalid field types deny writes.
Errors expose structured diagnostics; see [Diagnostics and secrets](diagnostics-and-secrets.md).
`backend_info` exposes `selection_engine_state` (`off`, `ready`, `unsupported`),
`selection_engine_reason`, `filters_active`, and writer support. Both backend_info
and obsidian_vault_stats expose selection_only_source, selection_ignore_source
(always explicit/env-only), selection_use_ignore_files_source and
selection_ignore_files_source (`explicit` or `vault`); no settings values are echoed.
Journal refresh rereads the milestone; a changed policy is capability-checked
before writes, and old configuration verdicts cannot be reused.

Reads bypass all selection filters, including explicitly configured filters.
Devices retain local copies of files that later become ignored; the database
is the source of truth for peer reads. Explicit LIVESYNC_USE_IGNORE_FILES=false
is allowed even when the vault enables ignore files, but devices then drop
peer writes to ignored paths instead of reflecting them to disk.

A 4096-entry LRU caches supported allow/deny verdicts by canonical path, rules
fingerprint, case sensitivity, and ignore-content fingerprint. CouchDB keeps it
in process memory; the journal backend persists it in its local SQLite store
and retains it across dedupe resets. Rules changes invalidate old entries.
Ignore files are read before lookup, so content changes cannot reuse a stale verdict. Cache hits
avoid engine calls; misses reuse the worker instead of spawning Node per write.
The standalone synchronous `livesync_format.is_target_file` convenience API
retains one-shot execution for isolated callers/tests with no backend lifecycle;
backend writes and diagnostics never fall back to it.

Local macOS / Node 26.13.1 measurements (100 warm regex-only checks; no vault
network I/O) give the following median / maximum latency in milliseconds:

| Path | Median / max (ms) |
|---|---|
| Previous one-shot runner | 129.980 / 193.564 |
| Persistent worker, uncached | 0.211 / 0.454 |
| Process-memory cache hit | 0.0069 / 0.0615 |
| Journal worker + SQLite, uncached | 0.325 / 2.205 |
| Journal SQLite cache hit | 0.0369 / 0.1515 |

Worker cold startup including version check/self-test took 238.5 ms (paid at
active-filter startup). Peak child RSS was 45.9 MiB versus 45.8 MiB for one-shot;
the worker retains its memory between calls. Storage and ignore-file reads add
their own latency. Reproduce with `PYTHONPATH=. ./venv/bin/python
scripts/benchmark_selection.py MODE`, where MODE is `one-shot`, `worker`,
`cache`, `journal-worker`, or `journal-cache`.

Missing or unreadable ignore files are treated as absent, matching upstream.
Ignore files are ordinary vault metadata, including hidden `.gitignore` files stored under the `i:` prefix;
the peer reads their content through the backend, without exposing hidden files
through public note tools. Files are read through for each write, so changes
seen by CouchDB or the S3 peer's refreshed local state take effect immediately.
The nearest ancestor file with a matching rule decides; configured file order
breaks ties. Matching uses upstream minimatch with `dot`, `nocase`, `flipNegate`
on Linux paths. This resembles gitignore but differs: matching always ignores
case, leading `/` is not a root anchor, and a matching directory rule ending
in `/` returns immediately, preventing later negation. See the differential
fixtures for exact behavior.

Excluded create/update/delete and rename destinations return a clean error
before chunks or metadata are published. Existing excluded notes remain in
storage. As upstream does, renaming an excluded source to an allowed
path creates the destination while preserving the excluded source; an excluded
destination leaves the source unchanged. Copy checks the destination.
This is command preflight: the peer has no local filesystem copy at an excluded
destination. In contrast, a plugin rename **event after a disk move** classifies
selected-source/excluded-destination as source DELETE. The peer rejects that
command before making any storage changes, preserving its only copy.

## Ignore-name lookup and warnings

Both vault-derived and explicit comma-separated names use exact JavaScript trim and retain empty entries (targetFilter.ts:95-145). Empty, repeated-comma, absolute-looking, traversal-looking and backslash-containing names never refuse startup. Inactive useIgnoreFiles=false never validates or reads names. Missing, unreadable or unusable names contribute no rule. Storage lookup normalizes separators inside the vault-note namespace; escaping paths and service IDs are absent, never host-file reads. Backend info and vault_stats expose integer `selection_unresolved_ignore_names`: explicit names unresolved by the latest root/ancestor lookup; 0 while inactive or vault-derived. Correct the explicit name or publish its ignore file if this count is nonzero. Reads bypass filters.
