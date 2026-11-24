# Vault scans and text eligibility

### Vault scan limits

`LIVESYNC_SCAN_MAX_NOTES` (default `10000`) and `LIVESYNC_SCAN_MAX_BYTES`
(default `67108864`, 64 MiB) must be positive integers. They bound aggregate
UTF-8 body and metadata input to search, tags, graph, links and statistics.
At most two vault scans run concurrently. Partial responses include
`"truncated": true` and `scan` counts (`notes`, `bytes`, and configured maxima).
Previously list-shaped responses become `{ "results": [...], ... }` when
truncated. Counts and graph conclusions then describe only the scanned subset.
The CouchDB metadata index uses the same caps, retains no bodies or eden, and
propagates truncation to these tools. Direct note reads remain available.
These are aggregate scan limits. Plain-note HTTP responses are loaded before
actual size accounting. All text access additionally uses the per-file budget
below. Regex operations retain one disposable process per note; their deadline
is `LIVESYNC_REGEX_TIMEOUT_SECONDS` (default 10 positive finite seconds).
Regex replacement output is bounded by `LIVESYNC_REGEX_MAX_OUTPUT_BYTES`
(default 0 = effective text access budget); a resulting 0 means unlimited.
Output-size and deadline refusals are distinct and name their respective variable.
Literal replacement retains its existing behavior.

New and replaced metadata persist canonical, case-preserving paths: separators
are `/`, leading `./` and trailing slashes are removed, and internal parent
segments resolve (`folder/../b.md` becomes `b.md`). Parent traversal above the
vault root remains forbidden. Existing records are not migrated automatically.


## Vault-wide scan consistency

Search, tags, graph, listings, and statistics are best-effort scans in backend
iteration order. They do not guarantee a snapshot across the scan: CouchDB
changes can be interleaved between metadata windows and chunk fetches. Results
can therefore combine observations from different times; a successful mutation
means durable publication, not a promise that it remains the eventual winner.
The S3 journal backend also uses best-effort scans of its local state.
Refresh can interleave with a scan; epoch and remote-wipe recovery retain local
revision trees and chunks. An unreadable note can be returned without text.

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
`LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES` (default 4096, positive integer) caps
the number of cached documents with LRU eviction on both backends. Journal
verdicts live in SQLite (old caches are pruned on open) and survive dedupe
resets; CouchDB verdicts live in process memory and are cleared on
startup/close.
