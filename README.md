# obsidian-livesync-mcp

A Python MCP peer for an Obsidian [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync)
vault in CouchDB or S3 journals. It uses device-identical path identities,
splitters, chunk IDs and file-size admission at the [audited upstream pin](docs/upstream-pin.md),
with the explicit differences below. No Obsidian app or local vault folder is
needed on the server. Read, edit, search, manage properties/tags, links and canvas from an MCP client; see the [tool overview](docs/tools.md).

[Install and start](#quick-start) · [clients and authentication](docs/clients-and-authentication.md) ·
[connection recipes](docs/connecting-storage.md)

## Quick start

Requires Python 3.11+ and an existing vault that already syncs in LiveSync.
Node.js 22+ is additionally required only when selection filters are active.

```bash
git clone https://github.com/adeepn/obsidian-livesync-mcp
cd obsidian-livesync-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: choose the backend, copy its connection values from the plugin,
and set one authentication mode. Replace template secrets; unused backend/auth
groups should be removed or commented. `run.py` loads `.env` before constructing
the application; other launchers must supply the environment themselves.

| Backend | Copy from LiveSync settings | First access | Smoke check |
|---|---|---|---|
| CouchDB | URL, username, password, database | Direct database access | `obsidian_vault_stats`, then `obsidian_list_notes` |
| S3 journal | Endpoint/access key/secret/bucket/region/prefix; compatible `sls://` URI | Full journal replay into local state | Same calls; allow replay to finish |

For an already syncing vault, open **Settings → Self-hosted LiveSync → Remote
Configuration → Connection settings → Saved connections**, find the row marked
**(Active)**, then click its wrench (**Configure**). Copy the connection fields
locally and click **Cancel**; do not save or change the working connection.

Follow the [exact connection steps and .env blocks](docs/connecting-storage.md)
for either backend. Startup validates settings; S3 replay starts on the first
tool access. There is no dedicated health endpoint.

```bash
source venv/bin/activate
python run.py
```

Connect an MCP client to `http://localhost:8484/mcp` with the configured bearer
token. For a local deployment set `MCP_HOST=127.0.0.1` and
`MCP_SERVER_URL=http://localhost:8484`; use HTTPS/reverse proxy for remote access.
For Codex, set the same token in its environment:

```toml
[mcp_servers.obsidian]
url = "http://localhost:8484/mcp"
bearer_token_env_var = "MCP_API_KEY"
```

Missing credentials/auth, template secrets or incompatible vault settings fail
at application construction/startup. Configuration errors name variables and validation
rules without echoing supplied values, including invalid ports and backend names.
Restart after changing configuration.

## Configuration

See the [complete environment reference](docs/configuration.md).

| Variable | Default | Recovery |
| --- | --- | --- |
| `LIVESYNC_COUCHDB_MAX_INFLATED_BYTES` | Splitter-derived, 32 MiB floor | Raise and restart for legitimate larger HTTP/document responses; read-only POST remains a typed refusal; an unreadable write acknowledgement is outcome unknown: re-read before retrying. |

Legacy lossless text cuts and read-only reconnect ingestion comparison need no new environment variable.
They extend a cut one UTF-16 unit and reset the next piece to its full nominal size.

## Backends

### CouchDB

Use the same database as LiveSync. The peer reads and publishes live documents;
no local replica directory is required. [Connection details](docs/connecting-storage.md#couchdb).

### S3 journal

The peer replays append-only journals into SQLite (chunk documents included), then appends
new journals. Tools refresh automatically, subject to the refresh interval;
there is no background polling. [Connection details](docs/connecting-storage.md#s3-journal),
[refresh, publication and conflict semantics](docs/s3-journal.md).

For CouchDB JWT connections, follow the [JWT setup route](docs/connecting-storage.md#couchdb-jwt-authentication) to copy the plugin fields and configure the server verifier.

## Supported vault settings

“Identical” means source-audited behavior at the pin, not live acceptance of every
combination. See the [complete setting/value/status table](docs/peer-limitations.md#supported-vault-settings)
and [source and live evidence](docs/upstream-pin.md).

| Settings | Status | Details |
|---|---|---|
| Path normalization and filename case | Identical | Legacy/NFC, insensitive/sensitive identities |
| Splitter versions and legacy migration | Reconnect compares device-ingested bodies; lossless text cuts; otherwise identical where terminating and lossless | [Exact coercion and guards](docs/vault-format.md#invalid-writer-parameters) |
| Chunk hashes, Eden, long-line threshold | Identical at pin | [Full settings table](docs/peer-limitations.md#supported-vault-settings) |
| File size and ignore-file selection | Device-derived recommendation-only policy; local overrides; unusable name shapes add no rules, unresolved policy ambiguity refuses with the override name | [Size](docs/size-policy.md), [selection](docs/selection-filters.md) |
| Headers, JWT and addressing | Explicit local connection settings | [Connection setup](docs/connecting-storage.md) |
| CouchDB compression | Device-compatible; transformations disclosed; bounded reads; unreadable write acknowledgement is outcome unknown | [Compression](docs/vault-format.md#couchdb-compression) |
| E2EE and path obfuscation | Unsupported | [Recovery choices](docs/peer-limitations.md#supported-vault-settings) |
| Deletion retention and conflict automation | Deliberate differences | [Policies](docs/peer-limitations.md#deletion-and-conflict-policies) |
| Reflection, hidden/customization/plugin sync | Outside ordinary-tool scope | [Full scope](docs/peer-limitations.md#supported-vault-settings) |
| YAML aliases and properties | Supported with configurable projection budgets | [Properties](docs/frontmatter-properties.md) |
| HTTP request bodies | Configurable local policy | 1 GiB default, 0 unlimited; `LIVESYNC_MAX_REQUEST_BYTES`; [HTTP 413 recovery](docs/configuration.md#http-request-bodies) |
| CouchDB request purpose | Read-only POST remains a typed refusal | [Endpoint purposes](docs/vault-format.md#unreadable-write-acknowledgements) |
| Device request/cache/concurrency tuning | Local peer policies | [Operational bounds](docs/peer-limitations.md#operational-and-tool-bounds) |

## How the peer differs from a device

The [full inventory](docs/peer-limitations.md) retains exact defaults, coercion,
refusal conditions and recovery instructions. The main practical differences:

- E2EE and obfuscated paths require an unencrypted, unobfuscated vault.
  [Scope and alternatives](docs/peer-limitations.md#supported-vault-settings).
- Hidden files, customization and plugin namespaces are excluded from ordinary tools.
  [Path and reflection scope](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Size/ignore policies follow devices before PREFERRED; overrides win. Devices that agree give their common value without any timestamp. Only a disagreement looks at timestamps: the newest device wins, and equally newest devices that agree give their common value. A disagreement without a winner goes to PREFERRED, and so does a setting that no device advertises; if PREFERRED is silent too, the setting takes its device default, and only the unresolved disagreement becomes a typed ambiguity refusal that names the operator override. See [arbitration and provenance](docs/tweak-compatibility.md#recommendation-only-writer-policy).
- The peer is an invisible writer: no device registration or PREFERRED publication.
  [Tweak policy](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Deletions retain body/chunk history; no timed purge or automatic conflict merging.
  [Retention and conflicts](docs/peer-limitations.md#deletion-and-conflict-policies).
- Reads come from storage and bypass device selection/reflection filters.
  [Selection and size behavior](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Native nontermination, data loss, unsafe renames and chunk collisions refuse before publication.
  [Exact guards and remedies](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Legacy text cuts keep valid astral characters together by extending the piece
  one UTF-16 unit; the next piece uses its full nominal size. Binary cuts and reads
  are unchanged. Compression preserves CR/LF and leading BOM; CouchDB refuses
  lone-surrogate leaves. Caller-provided lone units retain Blob ingress replacement.
  [Transport and compression](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Successful lossy writes disclose typed notices. Unreadable write acknowledgements
  are outcome unknown; re-read before retrying, especially non-idempotent append.
  Endpoint purpose distinguishes read-only POST from publication; undeclared operations
  refuse before sending with upgrade guidance.
  [Publication and outcomes](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Replay has configurable resource budgets; runtime decoder/SQLite capacity also applies.
  [Limits and recovery](docs/peer-limitations.md#operational-and-tool-bounds).
- Whole-text access, scans and regex requests use separate configurable budgets and pagination.
  [Tool contracts](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Properties/tags have expanded YAML node, byte and depth budgets; raw text remains available.
  [Projection limits](docs/peer-limitations.md#how-the-peer-differs-from-a-device).
- Transport/selection deadlines, retries, auth and headers are configured locally.
  [Defaults and connection constraints](docs/peer-limitations.md#how-the-peer-differs-from-a-device). The read-only reconnect helper compares device-ingested bodies, types and Blob sizes. It reports `ingestion_transformed`, with `invalid_bytes_replaced` for malformed plain UTF-8 or `splitter_projection` for changes such as legacy BOM removal; local and remote sizes/SHA-256 remain visible. Non-plain files compare byte-exact. A per-file failure reports its cause class and continues. See the [live checklist](docs/live-check-parity-checklist.md).

## Outcome reporting

**Applied** confirms durable publication, not conflict winner status.
**Refused without change** leaves the requested mutation unapplied;
**skipped** means the new oversized input was not saved and any old revision
remains. **Outcome unknown** means metadata publication may have happened:
re-read affected paths before retrying, especially non-idempotent edits.
[Operation/retry contract and journal reconciliation](docs/outcome-reporting.md).

## Local state and recovery (S3)

Persist a dedicated `S3_STATE_DIR` per peer/vault. `journal.sqlite3` with its WAL/SHM
sidecars is a rebuildable replica with history, not an eviction cache; disk can exceed
vault size. `chunks/` holds files of releases before chunk documents, read only. Do not share it between processes.
Wipe clears replication tracking only; an epoch change alone preserves dedupe.
Path identity changes archive the whole old directory under
`<state_dir>.quarantine/<UTC timestamp and unique suffix>/state`, then rebuild.
To restore, stop the peer, move current state aside, restore the archived
`state` and the old identity settings. Monitor `obsidian_vault_stats.local_state`.
[Safe reset, survivors, disk warnings and full restoration steps](docs/local-state-and-recovery.md).

## Upstream tracking

[Pin and source/live evidence](docs/upstream-pin.md). The daily/manual watcher
checks plugin releases and commonlib protocol-manifest changes, maintaining
one `upstream-drift` issue; failures do not close alerts. Run
`scripts/check_upstream.sh` with authenticated `gh`. Review drift, extend tests
and update the pin by PR. Monthly scratch-bucket smoke:
[existing checklist](docs/live-check-s3-checklist.md); settings parity acceptance:
[owner checklist](docs/live-check-parity-checklist.md).

## Security, systemd, clients and authentication

Keep credentials and `.env` out of version control. Default logs/diagnostics
omit remote bodies, headers, secrets and settings values; note paths remain
visible with control characters escaped. Reporting and hosting boundaries log only
cause classes and typed fields, without exception chains. Keep unsafe debug disabled.
[Diagnostic policy](docs/diagnostics-and-secrets.md).

Use one auth mode: bearer `MCP_API_KEY`, or the complete OAuth group.
Bearer takes precedence if both are supplied. OAuth uses password + PKCE,
pre-registered Claude callbacks and resource binding to the configured URL
plus `/mcp`; DCR is disabled. [Systemd commands, Claude.ai setup and other clients](docs/clients-and-authentication.md).

## Configuration and tool reference

The [environment reference](docs/configuration.md) lists every variable, default,
requirement and override. Copy only the selected backend and authentication group
from [.env.example](.env.example); keep secrets local.

[Tool overview](docs/tools.md) · [text access and scan policy](docs/scan-policy.md) ·
[full size policy](docs/size-policy.md) · [oracle regeneration](docs/regenerating-oracle-fixtures.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).

At the hosting boundary, SDK transport/validation logs contain only logger name,
closed level and exception class. Request header values, paths, body fragments,
third-party messages and exception text/tracebacks are withheld before handlers.
This includes Content-Type, Host and Origin validation and resource/prompt errors.

CouchDB decompression has a per-document operational budget, derived from the
resolved splitter with a 32 MiB floor; override LIVESYNC_COUCHDB_MAX_INFLATED_BYTES
and restart for larger valid pieces. This is separate from device file admission.
An unreadable PUT/POST/DELETE acknowledgement is outcome unknown: the change
MAY have been applied. Re-read before retrying; append is NON-IDEMPOTENT.
Budget/encoding recovery details accompany the warning. Read refusals remain
ordinary typed refusals. An uncertain leaf PUT may leave an orphan; the note
remains unchanged until metadata publication. See [response outcomes](docs/vault-format.md#unreadable-write-acknowledgements).

Custom headers deliberately differ from the plugin for transport safety:
Content-Length, Transfer-Encoding, Host, Connection, Keep-Alive, TE, Trailer,
Upgrade and all Proxy-* fields are transport-owned and refused case-insensitively
for both storages. Remove these entries and configure routing/proxy transport
separately. Other headers still apply after S3 signing.

Active selection verifies installed vendor/worker hashes before execution;
restore or regenerate the verified installation after a provenance-mismatch.
