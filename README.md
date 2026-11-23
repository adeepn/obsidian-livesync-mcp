# obsidian-livesync-mcp

Remote MCP server for accessing your Obsidian vault via CouchDB / [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync). Works with Claude Code, Claude.ai, and any MCP-compatible client.

No Obsidian desktop app required on the server — connects to CouchDB or S3-compatible LiveSync journal storage.

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
- **obsidian_upload_attachment** — Upload PDFs, scans, and other binary attachments from base64 (32 MiB decoded maximum)
- **obsidian_download_attachment** — Download complete binary attachments as base64 with size/SHA-256 (32 MiB raw maximum; optional `max_bytes` limit, no truncation)
- **vault_stats** — Vault statistics
- **create_from_template** — Create from template with variable substitution
- **list_templates** — List available templates

## Prerequisites

- CouchDB with Obsidian LiveSync data, or S3-compatible LiveSync journal storage
- Python 3.11+

### CouchDB vault compatibility

Binary uploads and downloads are symmetrically capped at **32 MiB raw
(decoded) bytes**. Uploads are stored as LiveSync `newnote` documents with
per-chunk base64 data. `obsidian_download_attachment(path, max_bytes=0)` returns
JSON `{path, size, sha256, content_b64}` with base64 transport and SHA-256 over
the raw bytes. A positive `max_bytes` sets an additional caller limit; exceeding
either limit returns an error without content or truncation. The hard cap keeps
JSON-RPC payloads manageable. `obsidian_fetch` returns binary metadata including
`size` and `sha256`, without the body. Use text note tools for textual paths.

The server derives document ids from vault paths the same way LiveSync does.
Both backends support these vault settings:

- **No end-to-end encryption** — E2EE vaults are rejected at startup (and encrypted chunks are refused on read).
- **No path obfuscation** — obfuscated vaults are rejected at startup.
- **Either filename case setting** — document ids preserve case when
  `handleFilenameCaseSensitive` is on and are lowercased otherwise (the default).
- **Path normalization version 0 or 1** — legacy spelling (the default), or
  Unicode NFC, independently of the case setting.

A vault using an unsupported combination fails the startup check with an explicit error instead of serving mismatched data.

Writes use the upstream `v3-rabin-karp` splitter and `xxhash64` chunk IDs.
The milestone's preferred `minimumChunkSize` and `customChunkSize` control
byte boundaries; absent values default to 20 and 0. `longLineThreshold`
has no v3 effect. Text at least 4 MiB uses binary splitting thresholds
while retaining text chunks. Binary data is split before each piece is
base64-encoded. Empty content has no children; repeated pieces retain their
ordered references. Explicit older splitter versions and other hash
algorithms are refused at startup. Existing chunks remain readable under
their original IDs; a fresh write derives the upstream IDs.


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

### S3 journal backend

Binary uploads and downloads are symmetrically capped at **32 MiB raw
(decoded) bytes**. Uploads are stored as LiveSync `newnote` documents with
per-chunk base64 data. `obsidian_download_attachment(path, max_bytes=0)` returns
JSON `{path, size, sha256, content_b64}` with base64 transport and SHA-256 over
the raw bytes. A positive `max_bytes` sets an additional caller limit; exceeding
either limit returns an error without content or truncation. The hard cap keeps
JSON-RPC payloads manageable. `obsidian_fetch` returns binary metadata including
`size` and `sha256`, without the body. Use text note tools for textual paths.

Set `LIVESYNC_BACKEND=s3` and choose exactly one connection form.
Use the URI or individual-variable block in the quick start matrix.
Prefix is prepended literally to object keys; include a trailing slash for a
folder prefix. Shared state settings work with either connection form.

Before each tool operation, the backend refreshes its local state. On first
use, this replays the full journal history. This one-time replay can take a
long time for a large vault. Startup validates settings and opens local state; it does not replay
journals. Subsequent refreshes use the processed-file ledger; epoch changes or a detected
remote wipe reset the cache and require replay again. Use a dedicated writable
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

Replay uses bounded downloads (512 MiB compressed per object), inflation (1 GiB),
records (100,000 per file), lines (64 MiB in UTF-8 bytes), and JSON nesting
(512 levels). Limits are module constants in `object_storage.py` and
`journal_codec.py`; `Boto3ObjectStorage(max_object_bytes=...)` and the codec
keyword arguments allow overrides. `JournalBackend` also accepts
`max_inflated_bytes`, `max_records`, and `max_line_bytes`.

Each refresh examines at most 1,000,000 keys, downloads at most 4 GiB
(including sync parameters), and has a 300-second wall-clock budget.
Override these through `JournalBackend(max_refresh_keys=...,
max_refresh_bytes=..., max_refresh_seconds=...)`. These are Python constructor
options, not environment variables. Stops are logged; processed files stay
processed. Key-limited sweeps continue from their last listed key, then restart
from the beginning so late objects below the cursor are eventually discovered.
Files are numerically ordered within each bounded window. Byte/time stops retry
the unfinished window and skip its processed files. The time budget is checked
between synchronous operations; an in-flight request, parse or SQLite transaction
can finish after the deadline. Configure budgets large enough for a single
allowed journal plus control reads. Restarting the server restarts the listing
sweep; the durable processed ledger preserves replay progress.

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
objects. Local cache resets can delete local state.

Concurrent edits can leave sibling revisions. The winner is deterministic:
highest revision generation, then lexicographically greatest revision hash.
A successful write confirms publication, not that it will remain the winner.
Sibling revisions remain available to LiveSync's conflict UI in Obsidian for
resolution.

Supported S3 vault settings are legacy path identity (normalization version 0)
and NFC identity (version 1), each with case-insensitive or case-sensitive
filenames. Both backends follow the vault setting. NFC aliases composed and
decomposed names even in sensitive mode. Existing local state with another
identity requires rebuilding; incompatible metadata IDs are refused. E2EE
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
2. Check the exact configured path. Move the directory aside for recovery, or
   remove only that directory with `rm -rf -- "$S3_STATE_DIR"` after verifying
   that the variable is set and points to the intended peer state directory.
3. Start the server (`sudo systemctl start obsidian-mcp` for systemd). The next
   tool call replays remote journals to rebuild local state; allow time and disk
   for a full replay. Check statistics and known notes before discarding a backup.

A reset removes local replay progress; it does not delete remote journals.
Automatic logical resets occur on a journal epoch change
(`protocolVersion:pbkdf2salt`) or when the last journal published by this peer
vanishes, indicating a remote wipe. These clear local tables/chunks and require
replay again; they are compatibility/recovery actions, not space-based eviction.
Changing path-identity mode requires a fresh state directory; it fails closed
rather than silently converting the existing replica.

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
These are aggregate scan limits: an individual HTTP response or note is still
loaded before its actual size can be checked. Regex replacements have a separate
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
Refresh or state resets can interleave with a scan; chunks are retained until
an epoch or remote-wipe reset. An unreadable note can be returned without text.
