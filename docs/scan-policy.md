# Vault scans and text eligibility

## Complete scans, optional budgets

The peer has no default aggregate note or byte cap. `LIVESYNC_SCAN_MAX_NOTES`
and `LIVESYNC_SCAN_MAX_BYTES` default to `0` (unlimited); positive integers
bound admitted source notes and UTF-8 metadata/body input. These controls are
separate from the whole-note text access budget below and the device's file-size
policy. Upstream commonlib `serviceModules/ServiceFileAccessBase.ts:287-290`
returns every file; these peer controls never redefine vault eligibility.

`LIVESYNC_SCAN_MAX_SECONDS` defaults to `20`, accepts nonnegative finite seconds,
and uses `0` for unlimited. Elapsed time includes waiting for one of two scan
slots. A call unable to acquire a slot before its deadline returns typed
`reason: scans_busy` with retry guidance naming the variable. Time checks use
an injectable monotonic clock for deterministic tests.

Every aggregate scan returns `scan: {notes, bytes, elapsed_ms, max_notes,
max_bytes, max_seconds, stopped_because}`. A complete call has a null stop
reason. An early stop additionally returns `truncated: true` and `next_cursor`;
`stopped_because` is one of `notes`, `bytes`, `time`, `index`, or `limit`.
Former list-shaped tool results use a `results` envelope, including complete
calls; message-only results use `message`, so all can carry scan metadata.
Existing result-field names and meanings remain otherwise unchanged.

## Cursors, limits and composition

Every aggregate tool accepts opaque `cursor: str = ""`. Pass `next_cursor`
from the preceding response without editing it. Each page processes display
paths sorting strictly after the last fully processed path. Both backends
iterate text in display-path order; metadata listings use that order too.
On a static vault with a complete metadata index, consecutive pages have no
missing or duplicate source notes. This is not a snapshot: notes created or
renamed behind the cursor between calls can be missed or repeated.

`obsidian_search` accepts `limit=200` matching notes per page; `0` is unlimited.
List, tags, graph, links, orphan, hub and template tools accept `limit=200`
source notes per page; `obsidian_vault_stats` accepts `limit=0` by default.
A positive limit stops a page with reason `limit` and a continuation cursor.
With a complete metadata index, one source note's outgoing links are processed completely before moving
the cursor. Search pages contain that page's matches; tag pages contain that
page's counts (sum them). Graph pages contain links discovered in that page's
source notes (concatenate edges). Backlinks, orphans and hubs explicitly say
`scanned pages only`: they cannot prove whole-vault conclusions from a partial
page. See [the per-tool table](tools.md#aggregate-tool-pagination).

## Resumable CouchDB metadata index

Index enumeration follows upstream commonlib `pouchdb/LiveSyncLocalDB.ts:481-536`
full paged enumeration rather than silently hiding notes beyond a fixed cap.
The peer caches collected IDs, next range/skip offset and compact indexed
metadata in memory, keyed by database `update_seq`. Later calls continue
incomplete work. A changed sequence restarts the index; bodies, chunks and Eden
are never retained in metadata. Optional note/byte limits and the time budget
bound each indexing call. While incomplete, tools serve available indexed
metadata and return `truncated: true`, reason `index`; vault counts are lower
bounds, never a complete-vault claim.

Vault stats expose typed `index_complete`, `index_ids_collected`,
`index_docs_indexed`, and `index_next_skip`, together with the configured scan
budgets. Cold-index pages need special care: CouchDB ID order can differ from
display-path order, so an incomplete index may later discover paths behind an
already returned cursor. Graph resolution can also lack targets not yet indexed.
Finish indexing and restart pagination without a
cursor before relying on full-vault composition.

## Fetch line windows

`obsidian_fetch` accepts optional `start_line` / `end_line`: positive, 1-based,
inclusive bounds, with either omitted. It first reads and validates the entire
text against the text access budget, then uses `str.splitlines(keepends=True)`
and returns the requested window plus `total_lines`, `returned_lines`, and
`truncated`. An end beyond the last line is clamped by normal slicing. With
both parameters omitted the existing whole-note content and response shape
are preserved. Properties and statistics describe the full original note.

## Persistent regex isolation

Regex search and replacement share one lazily started persistent isolated
worker per server process, using JSON-lines pipes. Each request is stateless;
the worker receives only pattern, text, operation, replacement and flags. It
inherits no listening sockets or credential environment. The per-request hard
deadline is `LIVESYNC_REGEX_TIMEOUT_SECONDS` (default 10 positive finite
seconds). A timeout or crash terminates and reaps the child; the next request
starts a fresh worker. Shutdown and abrupt event-loop closure also close it.
A timed-out search note appears in `errors` for that path and scanning continues.
A timed-out replacement refuses before writing and names the deadline variable.
Diagnostics never echo pattern or body text. Pattern validation, result shapes
and the maximum of ten matches per note remain unchanged.

Replacement output is bounded by `LIVESYNC_REGEX_MAX_OUTPUT_BYTES` (default
0 = effective text access budget); a resulting 0 means unlimited. Size and
deadline refusals are distinct and name their respective variable. Literal
replacement retains its existing behavior.

## Consistency and paths

Scans do not guarantee a snapshot: CouchDB changes can interleave between
metadata windows and chunk reads, and journal refresh can interleave with local
state scans. Epoch/wipe recovery retains local revision trees and chunks.
Unreadable notes appear in `skipped`. Successful publication means a durable
write, not a promise that it remains the eventual winner.

New and replaced metadata persist canonical, case-preserving paths: separators
are `/`, leading `./` and trailing slashes are removed, and internal parent
segments resolve (`folder/../b.md` becomes `b.md`). Parent traversal above the
vault root remains forbidden. Existing records are not migrated automatically.

### Text scan eligibility

Vault text scans visit every visible note, independent of extension. Plain
documents and textual newnotes share one decoded UTF-8 byte budget. Newnotes are read one independently
base64-encoded piece at a time with strict incremental UTF-8 validation; NUL
and non-text controls (except tab, CR and LF) reject the file immediately.
`LIVESYNC_TEXT_SCAN_MAX_BYTES` is an optional nonnegative integer: explicit 0
means unlimited, and any explicit positive bound wins even below the vault
policy. Unset/empty means max(positive `syncMaxSizeInMB` converted to bytes,
268435456). Conversion multiplies the binary64 MiB value by 1024 twice, then
floors the result, matching the device size predicate's integer-byte boundary
(commonlib `services/base/VaultService.ts:73-81`). Nonpositive vault policies
use the 256 MiB floor. An overflowing positive binary64 product means
unlimited (0), matching the device predicate. Resolution follows the current backend size policy,
including fractional values and every startup re-resolution. This is separate
from aggregate limits and applies to direct fetch/edit/canvas/properties/tags/
template reads as well as text scans, for both storage types.
Within budget, operations use the whole text. Beyond budget, fetch returns
metadata plus `content_available: false`, actual `size_bytes`, `budget_bytes`,
`reason: text_budget_exceeded` and a remedy naming the variable; edits raise
`TextBudgetExceeded` with the same fields, leaving the revision unchanged.
Complete byte download remains available for either storage type; the caller's
`max_bytes` still applies. See the [tool matrix](../README.md#text-access-tool-matrix).
`vault_stats` reports `text_budget_bytes`, `regex_max_output_bytes` and
`regex_timeout_seconds` as numbers. Rejected and oversized files appear in
`skipped`, and still count against the overall scan note budget. No extension
allowlist is used. Fetch and byte downloads retain the stored document type.

A normal PNG needs one piece read; a PDF with an ASCII prefix needs pieces
until the first binary/control byte or the byte budget; a textual `.base`
needs all its pieces, bounded by that budget. With `k` all-text prefix pieces,
a later binary PDF piece costs `k + 1` reads; a wholly textual PDF under budget
is eligible by the same rule as any other newnote. Negative verdicts avoid body
reads on later scans of the same revision. Positive verdicts are not cached:
querying text requires streaming the body again. Each document retains at most
one negative verdict for its revision and byte budget; superseded verdicts are
dropped on rewrite/replay or when a new revision/budget is observed.
`LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES` (default 65536, positive integer) caps
the number of cached documents with LRU eviction on both backends. Journal
verdicts live in SQLite (old caches are pruned on open) and survive dedupe
resets; CouchDB verdicts live in process memory and are cleared on
startup/close.
