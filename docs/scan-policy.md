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
actual size accounting; newnote text projection additionally has the per-file
piece budget described below. Regex replacements have a separate
10 MiB UTF-8 output cap and a two-second disposable-process deadline.

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
documents retain their text semantics. Newnotes are read one independently
base64-encoded piece at a time with strict incremental UTF-8 validation; NUL
and non-text controls (except tab, CR and LF) reject the file immediately.
`LIVESYNC_TEXT_SCAN_MAX_BYTES` (default 8388608, positive integer) bounds
newnote bytes decoded per file. Rejected and oversized files appear in
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
