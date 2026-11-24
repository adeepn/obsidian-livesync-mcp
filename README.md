# obsidian-livesync-mcp

Remote MCP server for accessing your Obsidian vault via CouchDB / [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync). Works with Claude Code, Claude.ai, and any MCP-compatible client.

No Obsidian desktop app required on the server — connects to CouchDB or S3-compatible LiveSync journal storage.

## Diagnostics and secrets

Tool diagnostics contain note paths as you supplied them, known setting key
names, sizes/counts, outcome/retry guidance and cause **class names**. Path
controls (C0/C1, CR/LF/tab, U+2028/U+2029) become visible escapes; paths are
never credential-redacted or shortened. Paths read from remote metadata also
use NFC normalization. Note contents remain note contents.

Diagnostics never include remote exception text, response bodies/headers,
configured URLs, buckets/regions, passwords, passphrases/salts, setting values,
selection patterns or ignore-file contents. Unknown keys are `<unexpected key>`;
unknown journal names expose only their length. Known journal filename shapes
and closed algorithm/enum names are allowed. `backend_info.epoch` is a short
SHA-256 fingerprint, never the journal salt. Local cache paths/database names
are hidden, remote update sequences are fingerprinted, and unfamiliar metadata
fields are counted rather than printed.

Server logs omit payloads and tracebacks by default. `LIVESYNC_UNSAFE_DEBUG=1`,
combined with a DEBUG log level, explicitly enables the bounded last-line debug
sink. **Unsafe for production:** it admits unstructured diagnostic inputs to a
debug path; never enable it to collect credentials. The current last-line
redactor inspects at most 2048 characters and withholds the entire nonempty
free-text value; tool diagnostics do not use this sink. This is deliberate: a detector
cannot reliably identify every possible secret encoding.

## Architecture

```
Obsidian + LiveSync <-> CouchDB or S3 journals <-> Python MCP peer <-> MCP client
```

The server reads and writes the same vault storage as the Obsidian plugin.
CouchDB holds the live documents; S3 holds append-only journals that this peer
replays into local SQLite revision trees and chunk files.

## Backends

Storage is selected by a single environment variable, `LIVESYNC_BACKEND`:

- **`couchdb`** — talks directly to the CouchDB instance your vault syncs to. Configure it with the `COUCHDB_*` variables below.
- **`s3`** — replays and publishes Self-hosted LiveSync journals in S3-compatible storage, with a local SQLite and chunk cache.

All tools work through a storage-agnostic backend interface, so switching backends later is a configuration change, not a rewrite. The `vault_stats` tool reports the active backend in its `backend` field.

## Upstream tracking

The daily and manually dispatched upstream watcher compares the latest
`vrtmrz/obsidian-livesync` release with [the compatibility pin](docs/upstream-pin.md)
and checks commonlib default-branch changes against the
[protocol manifest](docs/upstream-protocol-files.txt). It maintains one open
`upstream-drift` issue, updates its comparison output, and closes it when clean.
Check failures fail the workflow without closing an existing alert.

On drift, read the issue diff, update the pin through a PR, extend codec tests
or capture a new golden journal fixture from a real client, and note the
change in CHANGELOG. The initial commonlib tracking pin does not replace the
older audit or the plugin 1.0.50 live acceptance.

Run `scripts/check_upstream.sh` locally with authenticated `gh`. Once a month,
repeat the Task-10 live smoke against a **scratch bucket** using
`scripts/live_check_s3.py` (sweep-notes, verify-vault, write-phase,
verify-visible); follow the [live acceptance checklist](docs/live-check-s3-checklist.md).

## Features

### Notes
- **search** — Full-text search (text/regex)
- **fetch** — Get content, properties, and stats (.md/.base/.canvas)
- **create_note** — Create notes with frontmatter
- **update_note** — Modify (overwrite/append/prepend/after-heading/search-replace)
- **delete_note** — Delete notes
- **move_note** — Move/rename
- **duplicate_note** — Duplicate
- **list_notes** — List files with folder/extension filters

### Properties & Tags
- **manage_properties** — Get/set/delete frontmatter properties
- **manage_tags** — Add/remove/list tags
- **get_all_tags** — All vault tags with counts

### Graph & Links
- **get_links** — Outgoing/incoming links
- **get_link_graph** — Full vault graph
- **find_orphaned_notes** — Notes with no links
- **find_hub_notes** — Most connected notes

### Canvas
- **canvas_node** — Add/remove canvas nodes
- **canvas_edge** — Add/remove canvas edges

### Other
- **obsidian_upload_attachment** — Upload PDFs, scans, and other binary attachments from base64 (vault device size policy)
- **obsidian_download_attachment** — Download complete binary attachments as base64 with size/SHA-256 (optional caller `max_bytes` limit, no truncation)
- **vault_stats** — Vault statistics
- **create_from_template** — Create from template with variable substitution
- **list_templates** — List available templates

### Outcome reporting

Peer-initiated mutations have three outcomes:

- **Applied:** the backend confirmed durable publication. Journal writes also
  committed local metadata. Success does not guarantee eventual winner status
  against concurrent peers. Continue normally.
- **Refused without change:** policy skips/refusals, explicit CouchDB 4xx
  refusals, and proven connection failures before sending leave the requested
  mutation unapplied. Correct the refusal's cause before retrying.
- **Outcome unknown:** the result begins with `outcome unknown`. The change
  **MAY have been applied**, because the metadata response was lost after
  sending, publication verification failed, or local application failed after
  remote journal publication.

Uncertainty starts only when a request publishing the note's **metadata**
has been sent and its outcome is unknown (P-16). Losing a chunk PUT response
before metadata publication is an ordinary error: the note is unchanged; a
harmless orphan chunk may exist. Copy and move reuse chunks. If move publishes
the destination but loses the source-retirement metadata response, it reports
`destination published; source removal unknown — re-read both paths`.
An S3 journal publishes metadata and chunks together in one request. Force a
journal refresh before re-reading paths after an unknown outcome.

Every mutating tool names its operation and retry class:

- **NON-IDEMPOTENT:** append, prepend, search-replace, after-heading insertion,
  canvas node add and canvas edge add. A blind retry can duplicate the change:
  **re-read the note and apply the remaining change manually rather than
  retrying blindly**.
- **IDEMPOTENT FOR THE SAME INPUT:** overwrite, delete, move, copy, template
  create, note create, attachment upload, properties set/delete, tags add/remove
  and canvas node/edge remove. **Re-read to see whether it applied; retrying the
  same request is safe**. Create repeats may report “already exists”; tags add
  deduplicates. This describes the same request, including paths, values and IDs.

The journal peer commits a pending outbound key and payload digest locally
before sending, then confirms it after exact remote read-back. Replay/refresh
recognizes its own matching publications independently of `last_sent_key`,
applies and marks them processed, and repairs last-sent to the newest confirmed
outbound journal. Reconciliation is idempotent. An unpublished pending intent
is removed when its key is absent; a colliding key with different bytes is not
claimed. Removing an obsolete old key after reconciliation causes no refusal;
a missing newest confirmed journal still triggers wipe detection. Resets retain
local note revisions and chunks.

The preservation invariant applies to **peer-initiated mutations refused before
application**. It does not promise unchanged state after an unknown outcome.
Incoming replay reflects remote truth incrementally, like a device: journals
already applied remain applied if a later download fails. Retry refresh to
continue replay.

After confirmed remote publication/read-back and local metadata commit, journal
processed/last-sent tracking retries at most three times. Exhaustion logs a
warning and still returns success. Replay idempotently repairs an unmarked
journal and repairs last-sent tracking on the next refresh, without another write.

## Size behavior

Ordinary writes in **both backends**, including text edits and attachment uploads,
follow the device's `syncMaxSizeInMB`: skip only when `max > 0`, `size > 0`, and
`size > max * 1024 * 1024`. Equality is admitted; fractions work; zero/negative
disables the check. Raising 50 to 100 admits files through 100 MiB. Settings
resolve from milestone `tweak_values.PREFERRED`, otherwise the device maps
(conflicting limits refuse startup), with device default 50 when absent.
A blank/unset `LIVESYNC_SYNC_MAX_SIZE_MB` uses this vault value; an explicit
finite number overrides it. Settings are resolved at startup: restart this
peer after changing the vault setting. Other selection policies still apply.

Oversized ordinary input is **skipped**, before chunks or metadata are published;
any previous revision stays intact. Byte input is measured before optional
lossy text conversion; strings use Blob UTF-8 size. After conversion, the
Blob size is also checked before publication: lossy replacement may expand
raw input bytes. Either oversized quantity skips the write with previous
revision intact; the new content was NOT saved. Python backends signal `WriteSkipped` as a
control-flow outcome. Every MCP mutation tool handles it as JSON, for example:

```json
{"status":"skipped","path":"scan.pdf","size":43201331,"syncMaxSizeInMB":40,"message":"skipped: 41.2 MiB (43201331 bytes) exceeds the vault's syncMaxSizeInMB (40); new content was NOT saved; raise syncMaxSizeInMB in the plugin settings or pass a smaller file"}
```

This response does not claim success or persistence. MCP has no host vault file
to retain. Copy creates a new file and uses the size gate; delete remains allowed.
Rename with an oversized destination is refused before any mutation: source
revision/content and destination remain unchanged. The error names the size
and vault limit and states that the move was not performed. A device drops the
source from sync after moving its disk file; this peer has no host storage and
would lose the only copy (P-13). A literal same-path rename remains a no-op.
The upstream admission rule is
`C:managers/StorageEventManager/admission.ts:78-108,124-130`.
Received oversized revisions remain stored and readable, and existing DB
revisions are not excluded from journal packing. The plugin can skip reflecting
such revisions into its host vault until its settings allow them; this peer
reads the DB directly. Downloads have no device-size gate; `max_bytes` remains
an optional caller choice. Sources: [parity specification §д](docs/superpowers/specs/2026-11-23-settings-parity.md#д-syncmaxsizeinmb--file-selection-not-journal-admission).

The deliberate size/replay parity difference is **operational protection against
corrupt or hostile journals**. Devices have no corresponding replay budgets.
These budgets are independent of file size and `syncMaxSizeInMB`, including 0:

| Budget | Default | Environment override |
|---|---:|---|
| UTF-8 record line bytes | 256 MiB | `LIVESYNC_JOURNAL_MAX_LINE_BYTES` |
| Inflated journal bytes | 8 GiB | `LIVESYNC_JOURNAL_MAX_INFLATED_BYTES` |
| Records per journal | 1,000,000 | `LIVESYNC_JOURNAL_MAX_RECORDS` |
| JSON nesting levels | 512 | `LIVESYNC_JOURNAL_MAX_JSON_DEPTH` |
| Compressed object bytes (also control objects) | 8 GiB | `LIVESYNC_JOURNAL_MAX_OBJECT_BYTES` |
| Listed keys per refresh | 1,000,000 | `LIVESYNC_JOURNAL_MAX_REFRESH_KEYS` |
| Download bytes per refresh (also controls) | 64 GiB | `LIVESYNC_JOURNAL_MAX_REFRESH_BYTES` |
| Refresh elapsed seconds | 3600 | `LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS` |

All accept positive values without an imposed upper ceiling (integers except
seconds, which accepts fractions). A trip names the budget, observed amount,
configured limit, and exact variable to raise. Streaming inflate/download can
report a lower bound rather than consume an entire hostile body. Restart after
changing env values. Key/byte/time failures preserve completed replay progress;
subsequent throttled calls repeat the actionable diagnostic until a successful
refresh. Increasing budgets permits larger inputs but consumes more resources.

JSON depth has an additional decoder capacity: Python's standard JSON decoder
uses the interpreter recursion limit (normally 1000), minus current call-stack
headroom. Practical nesting capacity is therefore below roughly 1000 with the
default interpreter setting, even if `LIVESYNC_JOURNAL_MAX_JSON_DEPTH=20000`.
The override controls admission, not decoder capacity. Decoder exhaustion is a
`max_json_depth` budget error naming observed nesting, configured budget, exact
variable, and interpreter recursion limit; raising the variable alone cannot
remove that ceiling. Reduce nesting or use a decoder with greater capacity.
The default depth budget of 512 remains independent of file-size policy.

Default derivation: V3 binary pieces are at most 4 MiB; valid text may exceed its
boundary by 3 UTF-8 bytes. Base64 costs 4/3, JSON escaping at worst 6 per raw
byte (compact escaping at most 2). A conservative encoded-piece allowance is
32 MiB: `max(ceil(4 MiB/3)*4, (4 MiB+3)*6) < 32 MiB`. A line gets 8 allowances,
an inflated/compressed journal 256, and a refresh 2048. Metadata lines get the
same large allowance for child references; record/key defaults accommodate many
tiny pieces/objects. One hour permits 64 GiB at about 19 MiB/s; 512 nesting
levels far exceeds ordinary metadata (<10). These are headroom choices, not
protocol bounds. Legacy splitters, foreign peers and journals/history have no
universal maximum: unusually large inputs may require tuning even with these
defaults. No finite budget can promise admission of all unbounded histories.
See [replay_limits.py](replay_limits.py) for pinned source references.

## Prerequisites

- CouchDB with Obsidian LiveSync data, or S3-compatible LiveSync journal storage
- Python 3.11+

### CouchDB vault compatibility

Storage follows LiveSync device **disk ingestion**, with case-sensitive suffixes:
`.md .txt .svg .html .csv .css .js .xml .canvas` use host text reading and
`plain` chunks. Every other path (including `.MD`, `.json`, `.base`, `.yaml`,
`.png`, and extensionless files) uses `newnote` with independently base64-encoded
pieces, even when the content is textual. There is no content sniffing. Both
backends accept strings or bytes: strings for a newnote path become UTF-8 bytes;
bytes for a plain path must be valid UTF-8 or the write is refused before
publication. Upload callers may explicitly set `lossy_text=true` to replace
invalid bytes with U+FFFD; the response reports `replaced_bytes`, stored size,
and stored SHA-256. Newnote bytes remain unchanged. Native-host invalid-byte
behavior still needs live validation; opt-in replacement is this peer's policy.
The existing vault splitter determines pieces and their ordered child IDs;
metadata size is the ingested Blob byte length before splitting, including an
initial BOM. Legacy plain splitters remove that BOM from pieces while keeping
the original metadata size. Reads accept this mismatch, as the device local
database does. Device file reflection can refuse nonzero size mismatches unless
`processSizeMismatchedFiles` is enabled.

Tools expose a separate text representation: eligible newnotes can be fetched
and edited as text, regardless of extension. Their bytes must pass strict
UTF-8, control-character and per-file byte-budget checks.
Edits go through the same path-based writer, so `.base`/`.json`/`.yaml` stay
newnote. Frontmatter and canvas tools retain their existing format checks.
Ineligible newnotes return metadata (`size`, `sha256`) from `obsidian_fetch`;
use `obsidian_download_attachment` for their bytes. Newnote files that decode
as UTF-8 can also be downloaded without conversion.

Uploads use the vault's device size policy (see [Size behavior](#size-behavior)).
Their response size/SHA-256 describe the stored bytes; plain-path decoding and
splitting can change the input bytes. `obsidian_download_attachment(path,
max_bytes=0)` returns complete JSON `{path, size, sha256, content_b64}` over the
stored raw bytes. There is no peer file-size cap on downloads. A positive
`max_bytes` is an explicit caller limit: exceeding it returns an error without
content or truncation. Plain documents use `obsidian_fetch`.

The server derives document ids from vault paths the same way LiveSync does.
Both backends support these vault settings:

- **No end-to-end encryption** — E2EE vaults are rejected at startup (and encrypted chunks are refused on read).
- **No path obfuscation** — obfuscated vaults are rejected at startup.
- **Either filename case setting** — document ids preserve case when
  `handleFilenameCaseSensitive` is on and are lowercased otherwise (the default).
- **Path normalization version 0 or 1** — legacy spelling (the default), or
  Unicode NFC, independently of the case setting.

A vault using an unsupported combination fails the startup check with an explicit error instead of serving mismatched data.

Writes use the upstream `v3-rabin-karp` splitter and the vault's `hashAlg` chunk IDs
(`xxhash64`, `xxhash32`, `mixed-purejs`, `sha1`, or legacy `""`).
The milestone's preferred `minimumChunkSize` and `customChunkSize` control
byte boundaries; absent values default to 20 and 0. `longLineThreshold`
has no v3 effect. Text at least 4 MiB uses binary splitting thresholds
while retaining text chunks. Binary data is split before each piece is
base64-encoded. Empty content has no children; repeated pieces retain their
ordered references. Explicit older splitter versions allow startup, reads,
search and journal replay. All mutations
are refused with a tool error until that writer mode is implemented.
`backend_info` reports `writer_supported` and `writer_unsupported_reason`.
Existing chunks remain readable under their original IDs; a supported fresh
write derives the upstream IDs.


## Setup

### 1. Install

```bash
git clone https://github.com/adeepn/obsidian-livesync-mcp
cd obsidian-livesync-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env: pick LIVESYNC_BACKEND, fill the backend credentials,
# and set exactly one auth mode (see Authentication below).
```

`run.py` loads `.env` via python-dotenv before the server reads the environment. If you start the server any other way (systemd uses `EnvironmentFile=`), the variables must already be in the environment.

The server validates configuration when the application is constructed at import. Missing required variables, an absent auth mode, or placeholder secrets from `.env.example` fail immediately. `create_app(cfg)` accepts an injected validated `Config`; auth, URL, host, port, and backend startup all use that same configuration. Environment changes after construction require a new application or restart. OAuth still uses the preconfigured client registration and redirect URI rules below.

### Quick start matrix

Open **Obsidian → Settings → Self-hosted LiveSync** in a vault that already
syncs successfully. Use the connection settings for that same vault, not a new
bucket or database. Labels can vary with plugin version.

| Backend | Where credentials come from | Configuration | First tool call | Smoke check |
| --- | --- | --- | --- | --- |
| CouchDB | Plugin remote database settings: CouchDB URI, username, password and database name | CouchDB block below + shared server/auth block | Direct access, no full journal replay or local replica | Call `obsidian_vault_stats`, then `obsidian_list_notes` from an MCP client |
| S3 journal | Plugin object-storage/journal settings: **Copy setup URI** (`sls://…`), or endpoint, access key, secret key, bucket, region and prefix | Exactly one S3 block below + shared server/auth block | Full journal replay; time scales with history and network, disk roughly vault size or larger with history | Call the same two tools; allow replay to finish or continue across bounded refreshes |

Put the following shared settings in `.env`, replacing the deployment URL and
setting a strong bearer token locally (for example, generate one with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'`). Never share it.
For a local smoke check, these lines bind only to localhost:

```dotenv
MCP_API_KEY=<paste-generated-token>
MCP_SERVER_URL=http://localhost:8484
MCP_HOST=127.0.0.1
MCP_PORT=8484
```

For remote clients, configure your public HTTPS URL and reverse proxy; see
Connecting Clients and Authentication. Alternatively use the complete OAuth
group instead of `MCP_API_KEY`.

**CouchDB:** copy the four values from the plugin's remote database settings.
`COUCHDB_URL` is the server base URL; keep the database name in its own field.
Replace all angle-bracket fields before starting:

```dotenv
LIVESYNC_BACKEND=couchdb
COUCHDB_URL=<CouchDB-server-URI>
COUCHDB_USER=<plugin-username>
COUCHDB_PASSWORD=<plugin-password>
COUCHDB_DATABASE=<plugin-database-name>
```

> **Vault compatibility:** both backends support normalization version 0 or 1
> (Unicode NFC), each with case-insensitive or case-sensitive filenames.
> E2EE and path obfuscation remain unsupported.
> CouchDB needs no local state directory.

**S3 URI:** paste the plugin's object-storage `sls://` setup URI as one line.
An Obsidian-wide encrypted setup link is not an S3 connection URI.

```dotenv
LIVESYNC_BACKEND=s3
LIVESYNC_S3_URI=<paste-sls://-setup-URI>
```

**S3 individual fields:** use this instead of the URI block; copy the values
from the same plugin object-storage settings:

```dotenv
LIVESYNC_BACKEND=s3
S3_ENDPOINT_URL=<plugin-endpoint-URL>
S3_ACCESS_KEY_ID=<plugin-access-key>
S3_SECRET_ACCESS_KEY=<plugin-secret-key>
S3_BUCKET=<plugin-bucket>
S3_REGION=auto
S3_PREFIX=
```

Use the plugin's region and prefix if nonempty. Optional local state settings
are described under Local cache. Do not leave both S3 connection forms enabled,
even with empty individual values. If starting from `.env.example`, remove its
unused backend/auth settings and replace its placeholder secrets.

Start with `python run.py`, connect an MCP client to `http://localhost:8484/mcp`
using the bearer token, and call `obsidian_vault_stats` and `obsidian_list_notes`.
Expect the chosen `backend`, sensible note counts and known vault paths.
CouchDB access is immediate apart from the requested vault scan; S3's initial
replay can be lengthy. Startup validates settings but does not replay journals.
There is no dedicated health endpoint; these tool calls check storage access too.

### Environment reference

This table covers every environment variable consumed by `config.py`.
“Required for S3 fields” means only the individual-variable connection form;
URI and individual fields are mutually exclusive. Backend-specific settings
are read only for the selected backend. Empty defaults below mean an empty string.

| Variable | Default | Requirement | Purpose |
| --- | --- | --- | --- |
| `LIVESYNC_BACKEND` | none | Required | Select `couchdb` or `s3` |
| `COUCHDB_URL` | `http://localhost:5984` | Optional | CouchDB server base URL |
| `COUCHDB_USER` | none | Required for CouchDB | Database username |
| `COUCHDB_PASSWORD` | none | Required for CouchDB | Database password |
| `COUCHDB_DATABASE` | none | Required for CouchDB | LiveSync database name |
| `LIVESYNC_S3_URI` | empty | Required unless S3 fields set | `sls://access:secret@endpoint?bucket=…&region=…&prefix=…` connection |
| `S3_ENDPOINT_URL` | none | Required for S3 fields | S3-compatible endpoint URL |
| `S3_ACCESS_KEY_ID` | none | Required for S3 fields | Object-storage access key |
| `S3_SECRET_ACCESS_KEY` | none | Required for S3 fields | Object-storage secret key |
| `S3_BUCKET` | none | Required for S3 fields | Existing LiveSync journal bucket |
| `S3_REGION` | `auto` (fields); empty if omitted in URI | Optional | Object-storage region |
| `S3_PREFIX` | empty | Optional | Literal key prefix, including trailing slash if needed |
| `S3_STATE_DIR` | `./data` | Optional | Dedicated persistent local replica directory |
| `S3_REFRESH_MIN_INTERVAL_SEC` | `5` | Optional | Positive finite seconds between refresh attempts |
| `S3_STATE_SIZE_WARN_MB` | `0` | Optional | Non-negative integer warning threshold in MiB; 0 disables |
| `MCP_API_KEY` | empty | Required unless full OAuth group set | Shared bearer token; takes precedence over OAuth |
| `OAUTH_PASSWORD` | empty | Required for OAuth | Password on the authorization form |
| `OAUTH_CLIENT_ID` | empty | Required for OAuth | Pre-registered client ID |
| `OAUTH_CLIENT_SECRET` | empty | Required for OAuth | Pre-registered client secret |
| `MCP_SERVER_URL` | `https://localhost:8484` | Optional | External server base URL used for auth resource binding |
| `MCP_HOST` | `0.0.0.0` | Optional | HTTP bind address |
| `MCP_PORT` | `8484` | Optional | HTTP port, integer 1–65535 |
| `LIVESYNC_SCAN_MAX_NOTES` | `10000` | Optional | Positive note count cap per aggregate scan |
| `LIVESYNC_SCAN_MAX_BYTES` | `67108864` | Optional | Positive aggregate UTF-8 byte cap per scan |

### Device file selection

Set these local settings explicitly to match the device whose selection policy
this peer should use. They are never inferred from milestone/PREFERRED tweaks.
Defaults select every path permitted by the existing ordinary-file write gate.

| Variable | Default | Meaning |
|---|---|---|
| `LIVESYNC_SYNC_ONLY_REGEX` | empty | Require any allow expression to match when the parsed list is nonempty |
| `LIVESYNC_SYNC_IGNORE_REGEX` | empty | Reject when any ignore expression matches |
| `LIVESYNC_USE_IGNORE_FILES` | `false` | Enable vault ignore files (`true` or `false`) |
| `LIVESYNC_IGNORE_FILES` | empty | Comma-separated vault-relative ignore file names, e.g. `.gitignore,.syncignore` |

Regex lists use **`|[]|`**, not commas or newlines. ASCII spaces and LF are
removed; quotes are literal, empty items are discarded, duplicates remain.
`!!` inverts an individual expression. Patterns use ECMAScript RegExp search
against the complete path, without implicit anchors, with `i` unless the vault
uses case-sensitive filenames. `/foo/g` is literal regex source, not JS literal
syntax; `g`, `y`, `u`, and `v` flags cannot be configured by these settings.
For example, `\.md$|[]|\.canvas$` allows either suffix.

Selection filters are **off by default**. Without Node.js the server is fully
functional with filters off; it never launches Node in that mode. With any
selection filter configured, both backends require **Node.js 22+** in the service
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
Errors expose structured diagnostics; see [Diagnostics and secrets](#diagnostics-and-secrets).
`backend_info` exposes `selection_engine_state` (`off`, `ready`, `unsupported`),
`selection_engine_reason`, `filters_active`, and writer support.

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

### S3 journal backend

Storage follows LiveSync device **disk ingestion**, with case-sensitive suffixes:
`.md .txt .svg .html .csv .css .js .xml .canvas` use host text reading and
`plain` chunks. Every other path (including `.MD`, `.json`, `.base`, `.yaml`,
`.png`, and extensionless files) uses `newnote` with independently base64-encoded
pieces, even when the content is textual. There is no content sniffing. Both
backends accept strings or bytes: strings for a newnote path become UTF-8 bytes;
bytes for a plain path must be valid UTF-8 or the write is refused before
publication. Upload callers may explicitly set `lossy_text=true` to replace
invalid bytes with U+FFFD; the response reports `replaced_bytes`, stored size,
and stored SHA-256. Newnote bytes remain unchanged. Native-host invalid-byte
behavior still needs live validation; opt-in replacement is this peer's policy.
The existing vault splitter determines pieces and their ordered child IDs;
metadata size is the ingested Blob byte length before splitting, including an
initial BOM. Legacy plain splitters remove that BOM from pieces while keeping
the original metadata size. Reads accept this mismatch, as the device local
database does. Device file reflection can refuse nonzero size mismatches unless
`processSizeMismatchedFiles` is enabled.

Tools expose a separate text representation: eligible newnotes can be fetched
and edited as text, regardless of extension. Their bytes must pass strict
UTF-8, control-character and per-file byte-budget checks.
Edits go through the same path-based writer, so `.base`/`.json`/`.yaml` stay
newnote. Frontmatter and canvas tools retain their existing format checks.
Ineligible newnotes return metadata (`size`, `sha256`) from `obsidian_fetch`;
use `obsidian_download_attachment` for their bytes. Newnote files that decode
as UTF-8 can also be downloaded without conversion.

Uploads use the vault's device size policy (see [Size behavior](#size-behavior)).
Their response size/SHA-256 describe the stored bytes; plain-path decoding and
splitting can change the input bytes. `obsidian_download_attachment(path,
max_bytes=0)` returns complete JSON `{path, size, sha256, content_b64}` over the
stored raw bytes. There is no peer file-size cap on downloads. A positive
`max_bytes` is an explicit caller limit: exceeding it returns an error without
content or truncation. Plain documents use `obsidian_fetch`.

Set `LIVESYNC_BACKEND=s3` and choose exactly one connection form.
Use the URI or individual-variable block in the quick start matrix.
Prefix is prepended literally to object keys; include a trailing slash for a
folder prefix. Shared state settings work with either connection form.

Before each tool operation, the backend refreshes its local state. On first
use, this replays the full journal history. This one-time replay can take a
long time for a large vault. Startup validates settings and opens local state; it does not replay
journals. Subsequent refreshes use the processed-file ledger. A confirmed remote
wipe clears only deduplication tracking and starts replay from the beginning;
local notes and chunks stay readable. Use a dedicated writable
state directory per vault/peer and persist it across restarts. It contains
`journal.sqlite3` (SQLite revision state and processed journals) and
`chunks/` (cached chunk files).

`obsidian_refresh` lists remote journals and replays unseen files.
`S3_REFRESH_MIN_INTERVAL_SEC` is the minimum spacing between refresh attempts, including failures
and partial budget stops (positive seconds, default 5); calls within that interval are skipped.
Ordinary tool calls refresh automatically using the same throttle. There is
no background polling. `obsidian_refresh` also uses this throttle; allow the
interval to elapse to receive subsequent device changes. Internal callers can use
`refresh(force=True)` to bypass the throttle. Refresh failures
return a tool error instead of serving stale data. Calls throttled after an
unresolved failure also return an error; a successful refresh clears that failure.

Replay protection budgets are an explicit difference from the device, documented
under [Size behavior](#size-behavior). A trip returns an actionable error;
completed files stay processed. Key-limited sweeps continue from their last
listed key, then restart from the beginning so late objects below the cursor
are eventually discovered. Files are numerically ordered within each window.
Byte/time stops retry the unfinished window and skip its processed files.
The time budget is checked between synchronous operations; an in-flight request,
parse or SQLite transaction can finish after the deadline. Configure budgets
large enough for one journal plus control reads. Restarting the server restarts
the listing sweep; the durable ledger preserves progress.

Local state directories and chunk shards are private (`0700`); SQLite and newly
written chunk files use `0600`. Opening an existing cache tightens permissive
directory modes and logs each correction once.

Remote storage is append-only: updates publish new journal objects.
Publication first attempts atomic create-only `PutObject(IfNoneMatch="*")`
(Ruling B-13). Occupied keys are regenerated, with at most ten attempts;
read-back verification still precedes local application. Providers reporting
conditional writes as unsupported use the B-10 exists-precheck + put fallback,
with a warning logged once per adapter. That compatibility fallback retains
the race between the precheck and upload.
The backend never deletes S3 objects, prunes history, or runs remote garbage
collection. Deleting a note publishes a tombstone; it does not remove journal
objects. Automatic dedupe resets retain all local documents and chunks.

Concurrent edits can leave sibling revisions. The winner is deterministic:
highest revision generation, then lexicographically greatest revision hash.
A successful write confirms publication, not that it will remain the winner.
Sibling revisions remain available to LiveSync's conflict UI in Obsidian for
resolution.

Supported S3 vault settings are legacy path identity (normalization version 0)
and NFC identity (version 1), each with case-insensitive or case-sensitive
filenames. Both backends follow the vault setting. NFC aliases composed and
decomposed names even in sensitive mode. Existing local state with another
identity archives the old store in quarantine and starts fresh; incompatible
metadata IDs are refused. E2EE
and path obfuscation remain unsupported.

### Local cache (S3)

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

### 3. Run

With the virtualenv from step 1 activated:

```bash
python run.py
```

The server serves streamable HTTP at `/mcp`; all client URLs below assume that transport.

### 4. Systemd service (optional)

```bash
sudo cp obsidian-mcp.service /etc/systemd/system/
# Edit the service file: set User and paths
sudo systemctl daemon-reload
sudo systemctl enable obsidian-mcp
sudo systemctl start obsidian-mcp
```

## Connecting Clients

### Codex

Set the same API key in the environment where Codex runs, then add a streamable HTTP MCP server to `~/.codex/config.toml`:

```toml
[mcp_servers.obsidian]
url = "https://your-server/mcp"
bearer_token_env_var = "MCP_API_KEY"
```

Codex support has been verified with this streamable HTTP configuration and the bearer token stored in the `MCP_API_KEY` environment variable.

For MCP clients that ask for custom headers directly, use:

| Key | Value |
| --- | --- |
| `Authorization` | `Bearer your_api_key` |

Replace `your_api_key` with the exact value configured on the server as `MCP_API_KEY`.

### Claude.ai (Web)

1. Settings > Connectors > Add custom connector
2. URL: `https://your-server/mcp`
3. Client ID and Client Secret from your `.env`

## Authentication

The server supports two authentication modes, which are mutually exclusive:

1. **Bearer API key** - set `MCP_API_KEY` and clients must send `Authorization: Bearer <MCP_API_KEY>`.
2. **OAuth 2.1** - set `OAUTH_PASSWORD` plus the OAuth client settings to use the authorization code flow + PKCE.

Configure exactly one mode. If both `MCP_API_KEY` and the OAuth variables are present, the server runs in bearer mode only and the OAuth endpoints stay disabled. If neither mode is configured, the server fails at startup instead of running open.

In OAuth mode `OAUTH_PASSWORD` is enforced: `GET /authorize` first returns a password form, and an authorization code is issued only after the correct password is submitted (compared in constant time). The code still cannot be exchanged without `OAUTH_CLIENT_SECRET`, which must have high entropy.

Both auth modes bind access tokens to the configured `MCP_SERVER_URL` plus `/mcp`; OAuth refreshes retain that resource. Resource validation stays enabled. Client registration and redirect URI rules below are unchanged.

### Current limitations

Out of the box, OAuth only supports **Claude** (Claude.ai and Claude Code) as a client. The pre-registered OAuth client has hardcoded `redirect_uris` for Claude's callback URLs and `scope: claudeai`.

### Adding other MCP clients

To support other clients, **pre-register additional clients** — add more clients in `oauth_provider.py` `__init__` with the appropriate `redirect_uris` for your client.

Dynamic Client Registration (DCR) is disabled and should stay disabled: with it enabled, anyone who knows your server URL can register a client, and every registration grows the in-memory client store without bound.

## License

Apache 2.0

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

### Tweak compatibility at startup

Both backends read `tweak_values.PREFERRED` from the LiveSync milestone.
CouchDB uses `_local/obsydian_livesync_milestone`; S3 uses its milestone object.
Without PREFERRED the peer retains the existing device-settings fallback and
checks all advertised devices against that baseline. Startup compares the peer's
effective settings against PREFERRED and every device snapshot, and each device
against PREFERRED. Semantic differences refuse
startup with key names and an alignment instruction, without printing values.
Unadvertised settings remain distinct from known defaults. Legacy splitter
settings pass through the same one-pass migration used by the writer.

Fully advertised differences limited to `hashAlg`, `customChunkSize` and
`chunkSplitterVersion` can align automatically. The greater positive numeric
`tweakModified` wins; ties or two absent/nonpositive timestamps choose PREFERRED
(REMOTE). The winner is logged and applied to this peer's writer. This startup
check does not change device snapshots or publish PREFERRED. By explicit policy
this peer remains an invisible writer: it publishes note changes without those
milestone writes. CURRENT winners therefore still require remote alignment if
devices are to adopt their settings.

`LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK` defaults to `true`; `false` refuses
these differences. `LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH` defaults to
`false`; `true` bypasses ordinary semantic differences, matching the device's
local flag. ID derivation and path-normalization checks precede this gate.
Encryption and path-obfuscation refusals remain enforced. A CouchDB milestone
advertising compression also requires alignment: this peer has no CouchDB
compression adapter (journal framing does not use that option). These flags are local
policy, not values inferred from milestone tweaks. Restart after changing them.
`vault_stats` includes `tweak_assessment` with `status` and `differing_keys` only.

Source semantics and the deliberately read-only remote alignment policy are
recorded in the [parity specification](docs/superpowers/specs/2026-11-23-settings-parity.md#tweak-compatibility-and-peer-startup-requirements).
