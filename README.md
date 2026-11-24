# obsidian-livesync-mcp

A Python MCP peer for an Obsidian [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync)
vault in CouchDB or S3 journals. It uses device-identical path identities,
splitters, chunk IDs and file-size admission at the [audited upstream pin](docs/upstream-pin.md),
with the explicit differences below. No Obsidian app or local vault folder is
needed on the server. Read, edit, search, manage properties/tags, links and canvas
from an MCP client; see the [tool overview](docs/tools.md).

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
at application construction/startup. Restart after changing configuration.
`create_app(cfg)` also accepts a validated injected `Config`; that same object
controls auth, URL, host, port and backend startup.

### Environment reference


This table covers configuration consumed by `config.py` and `replay_limits.py`.
The final debug switch is consumed separately by `diagnostic_hygiene.py`.
Wire encoding follows devices without an environment switch: CouchDB JSON escapes
unpaired UTF-16 units losslessly; compact S3 journal leaves replace them with
U+FFFD and writes report `transport_replacements`. [Wire details](docs/s3-journal.md#surrogate-wire-encoding).
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
| `S3_REFRESH_MIN_INTERVAL_SEC` | `5` | Optional | Positive finite seconds below `2**63` between refresh attempts |
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
| `LIVESYNC_TEXT_SCAN_MAX_BYTES` | `8388608` | Positive integer | Per-newnote UTF-8 projection budget |
| `LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES` | `4096` | Positive integer | Negative verdict LRU bound |
| `LIVESYNC_SYNC_ONLY_REGEX` | `empty` | Optional | Local allow regex list, literal `|[]|` separators |
| `LIVESYNC_SYNC_IGNORE_REGEX` | `empty` | Optional | Local ignore regex list |
| `LIVESYNC_USE_IGNORE_FILES` | `empty` | Optional boolean | Empty/unset derives useIgnoreFiles from vault settings; true/false overrides |
| `LIVESYNC_IGNORE_FILES` | `empty` | Optional | Empty/unset derives ignoreFiles from vault settings; non-empty overrides comma-separated names |
| `LIVESYNC_SYNC_MAX_SIZE_MB` | `empty` | Finite number or empty | Override vault size policy; <=0 disables |
| `LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH` | `false` | Boolean | Bypass ordinary tweak differences, never identity/E2EE guards; CouchDB compression follows the vault independently |
| `LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK` | `true` | Boolean | Align fully advertised compatible-lossy tweaks |
| `LIVESYNC_JOURNAL_MAX_LINE_BYTES` | `268435456` | Positive integer <2**63 | UTF-8 record bytes |
| `LIVESYNC_JOURNAL_MAX_INFLATED_BYTES` | `8589934592` | Positive integer <2**63 | Inflated journal bytes |
| `LIVESYNC_JOURNAL_MAX_RECORDS` | `1000000` | Positive integer <2**63 | Records per journal |
| `LIVESYNC_JOURNAL_MAX_JSON_DEPTH` | `512` | Positive integer <2**63 | Nesting admission; decoder capacity is separate |
| `LIVESYNC_JOURNAL_MAX_OBJECT_BYTES` | `8589934592` | Positive integer <2**63 | Compressed/control object bytes |
| `LIVESYNC_JOURNAL_MAX_REFRESH_KEYS` | `1000000` | Positive integer <2**63 | Keys per refresh |
| `LIVESYNC_JOURNAL_MAX_REFRESH_BYTES` | `68719476736` | Positive integer <2**63 | Downloaded bytes per refresh |
| `LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS` | `3600` | Positive finite number <2**63 | Elapsed seconds, fractions accepted |
| `LIVESYNC_UNSAFE_DEBUG` | `unset` | Set to 1 plus DEBUG logging | Unsafe last-line diagnostic sink; keep disabled |

## Backends

### CouchDB

Use the same database as LiveSync. The peer reads and publishes live documents;
no local replica directory is required. [Connection details](docs/connecting-storage.md#couchdb).

### S3 journal

The peer replays append-only journals into SQLite and chunk files, then appends
new journals. Tools refresh automatically, subject to the refresh interval;
there is no background polling. [Connection details](docs/connecting-storage.md#s3-journal),
[refresh, publication and conflict semantics](docs/s3-journal.md).

## Supported vault settings

“Identical” means source-audited behavior at the pin, not live acceptance of every
combination. [Source inventory and caveats](docs/superpowers/specs/2026-11-23-settings-parity.md);
[real-client validation scope](docs/upstream-pin.md).

| Setting | Plugin values | Status | Peer behavior |
|---|---|---|---|
| `pathNormalizationVersion` | 0 legacy / 1 NFC | Identical | Both, independently of filename case; collisions refuse |
| `handleFilenameCaseSensitive` | false / true | Identical | Legacy, NFC, case-sensitive and NFC+case-sensitive identities |
| `chunkSplitterVersion` | v1, v2, v2-segmenter, v3-rabin-karp, legacy empty | Identical | All writers; legacy split-surrogate pieces retain device IDs and transport encoding; v2-segmenter equals v2 at this pin |
| `enableChunkSplitterV2`, `useSegmenter` | false / true (legacy migration) | Identical | One-pass migration of empty version; unknown version normalization follows upstream |
| `hashAlg` | xxhash64, xxhash32, mixed-purejs, sha1, empty | Identical | Upstream UTF-16 length and hash semantics |
| `customChunkSize` | Number >=0 in UI (default 0) | Identical wherever the plugin terminates | V1 plain cap zero advances one UTF-16 unit; V3 clamps; see [loop refusals](docs/vault-format.md#invalid-writer-parameters) |
| `minimumChunkSize` | Numeric (default 20) | Identical wherever the plugin terminates | Negative, empty, null and NaN follow upstream coercion; only V2 nonempty plain minima coercing to positive zero loop |
| `syncMaxSizeInMB` | UI number >=0, default 50; 0 disables | Deliberate difference | Device ordinary-write predicate; oversized rename refuses to protect source |
| `syncOnlyRegEx`, `syncIgnoreRegEx` | ECMAScript pattern lists | Identical when configured | Set local env explicitly; never inferred from milestone |
| `useIgnoreFiles` | Boolean | Identical | Derived from vault settings unless overridden; active policy requires Node.js 22+ |
| `ignoreFiles` | Filename list | Identical | Derived from vault settings unless overridden; upstream glob/rule order |
| `useEden` and count/length/age controls | Boolean / numbers | Identical | Current upstream writes empty Eden regardless; historical inline chunks readable |
| `longLineThreshold` | Numeric (default 250) | Identical | Unused by dispatched algorithms at this pin |
| `encrypt`, `E2EEAlgorithm`, `useDynamicIterationCount`, `encryptInternalMetadata` | E2EE options | Not supported for E2EE | Encrypted vaults fail startup; no encryption/decryption |
| `usePathObfuscation` | false / true | Not supported when true | Startup refuses; see issue #4 |
| `idDerivationVersion`, `idDerivationKey` | 0 legacy / 1 keyed | Identical for unencrypted vaults | Validate version/key configuration; unencrypted IDs follow upstream |
| `enableCompression` | false / true | Identical | CouchDB always reads compressed data; writes follow the vault (absent: false). Non-lossy CR/LF, surrogate and BOM exceptions below; journal forces off |
| `suspendParseReplicationResult` | false / true | Deliberate difference | Reads remain available from DB; no filesystem reflection stage |
| Hidden File Sync / customization sync | `i:`, `ix:` namespaces | Not supported | No local vault folder; hidden ignore files can be read internally |
| Plugin sync / `usePluginSyncV2` | `ps:` namespace / Boolean | Not supported | Service documents excluded from ordinary tools |
| Tweak mismatch/auto-accept flags | Boolean | Deliberate difference | Local env policy; no device registration or PREFERRED publication |
| Reflection/request/cache/concurrency tuning | Various local device options | Not supported as device settings | No host reflection or plugin request engine; [full inventory](docs/superpowers/specs/2026-11-23-settings-parity.md#ж-other-tweak_values-and-production-evidence-boundary) |

## How the peer differs from a device

- CouchDB compression uses Python zlib raw deflate level 8; bytes may differ
  from device fflate output, but devices read either stream. The peer preserves
  CR/LF text instead of treating line-wrapped base64 as binary (upstream loses
  those line breaks). It publishes data containing surrogate code points or
  a leading BOM uncompressed to prevent Blob conversion/decoding losses.
  Reads of existing compressed text match device Blob.text(), including U+FFFD
  replacement and BOM removal. Invalid base64/deflate refuses with a sanitized
  error: restore the document from a healthy device or backup. See
  [compressed document format](docs/vault-format.md#couchdb-compression).

- E2EE and obfuscated paths are unsupported; use an unencrypted, unobfuscated
  vault until [#3](https://github.com/adeepn/obsidian-livesync-mcp/issues/3) /
  [#4](https://github.com/adeepn/obsidian-livesync-mcp/issues/4) are implemented.
- Replay has operational byte/count/depth/time budgets. Named
  `ReplayBudgetExceeded` errors identify the env override to raise; Python JSON
  decoder capacity is a separate ceiling. [Budget defaults and recovery](docs/size-policy.md).
- Rename into an excluded or oversized destination refuses before mutation:
  the peer has no disk copy at the destination and must protect the only source.
  Choose an admitted path/size. Invalid UTF-8 on plain paths also refuses unless
  attachment upload explicitly opts into `lossy_text=true` replacement.
- Remote wipe recovery retains local revisions/chunks. Local-only survivors stay
  readable and are not republished automatically; account for them before reset.
- Ignore-file selection derives from vault settings unless independently overridden.
  Active selection requires Node.js 22+, including vault useIgnoreFiles=true.
  LIVESYNC_USE_IGNORE_FILES=false is allowed, but devices drop peer writes to
  ignored paths. Reads bypass all filters, including explicitly configured filters;
  devices retain local files that later become ignored, and the database remains
  the read source of truth. With filters off no Node is launched.
  [Filter syntax](docs/selection-filters.md).
- There is no local vault folder, Hidden File Sync, customization/plugin sync or
  suspended filesystem reflection. DB reads include received oversized files;
  a device may decline to reflect those files until its settings permit them.
  Plugin encrypted setup exports and CouchDB JWT auth are not supported; copy
  connection fields and use CouchDB username/password instead.
  Ordinary tools reserve service prefixes/names and dot-prefixed paths;
  [path admission](docs/vault-format.md#path-admission) lists the exact exclusions.
- Refuse only inputs on which the plugin itself does not terminate: V1
  nonempty binary/non-plain text with nonpositive caps, V1 nonempty plain text
  with negative caps, V2 nonempty binary with nonpositive caps, V2 text groups
  containing more than one Unicode code point with nonpositive caps (or empty
  groups with negative caps), and V2 nonempty decoded plain text with
  `minimumChunkSize` coercing to positive zero (ordinary 0, empty string or null);
  negative zero terminates.
  Use `customChunkSize` producing a positive pieceSize and a positive V2 plain
  `minimumChunkSize`. Empty inputs and V3 retain upstream semantics.
  [Exact cell evidence and recovery](docs/vault-format.md#invalid-writer-parameters).
- The peer is an invisible writer: it does not register a device or modify
  milestone/PREFERRED settings. Align device settings separately; review
  [tweak compatibility](docs/tweak-compatibility.md) after upgrades.
- Reads/search use strict UTF-8/control/budget projection for textual newnotes;
  scans are bounded, best-effort and may be truncated. Regex replacements have
  a 10 MiB output cap and two-second deadline. [Scan policy](docs/scan-policy.md).
- S3 custom endpoints use path-style addressing; virtual-host-only providers
  need an adapter change. Publication has an exists+put race fallback on stores
  without conditional writes. [S3 operation](docs/s3-journal.md).

## Outcome reporting

**Applied** confirms durable publication, not conflict winner status.
**Refused without change** leaves the requested mutation unapplied;
**skipped** means the new oversized input was not saved and any old revision
remains. **Outcome unknown** means metadata publication may have happened:
re-read affected paths before retrying, especially non-idempotent edits.
[Operation/retry contract and journal reconciliation](docs/outcome-reporting.md).

## Size behavior

Ordinary writes skip only if `max > 0 && size > 0 && size > max*1024*1024`.
Equality is admitted; fractions work; <=0 disables. Default is 50 MiB from
vault settings, unless explicitly overridden. Raw and converted Blob bytes are
checked before publication. Downloads have no device-size gate; `max_bytes`
is an optional caller limit. Restart after vault-setting changes.
[Full size policy, skips, protected renames and independent replay budgets](docs/size-policy.md).

## Local state and recovery (S3)

Persist a dedicated `S3_STATE_DIR` per peer/vault. `journal.sqlite3`, WAL/SHM
sidecars and `chunks/` form a rebuildable replica with history, not an eviction
cache; disk can exceed vault size. Do not share it between processes.
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
visible with control characters escaped. Keep unsafe debug disabled.
[Diagnostic policy](docs/diagnostics-and-secrets.md).

Use one auth mode: bearer `MCP_API_KEY`, or the complete OAuth group.
Bearer takes precedence if both are supplied. OAuth uses password + PKCE,
pre-registered Claude callbacks and resource binding to the configured URL
plus `/mcp`; DCR is disabled. [Systemd commands, Claude.ai setup and other clients](docs/clients-and-authentication.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
