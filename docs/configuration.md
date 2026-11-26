# Configuration

## Environment reference


Acceptance connection variables are test-only and belong in the invoking shell; see
[real-server acceptance](developer-acceptance.md#automated-real-server-acceptance).

This table covers configuration consumed by `config.py` and `replay_limits.py`.
The final debug switch is consumed separately by `diagnostic_hygiene.py`.
Wire encoding follows devices without an environment switch: CouchDB refuses
unpaired UTF-16 units before publication because the server rejects them; compact
S3 journal leaves replace them with U+FFFD and writes report `transport_replacements`. [Wire details](s3-journal.md#surrogate-wire-encoding).
“Required for S3 fields” means only the individual-variable connection form;
URI and individual fields are mutually exclusive. Backend-specific settings
are read only for the selected backend. Empty defaults below mean an empty string.

| Variable | Default | Requirement | Purpose |
| --- | --- | --- | --- |
| `LIVESYNC_COUCHDB_MAX_INFLATED_BYTES` | `empty` | Positive integer or empty | Empty derives splitter budget; bounds document inflation and HTTP wire/decoded responses; [operational policy](vault-format.md#couchdb-inflate-budget) |
| `LIVESYNC_MAX_REQUEST_BYTES` | `1073741824` | Nonnegative integer | HTTP request body bytes; 0 unlimited; HTTP 413 before tools when exceeded; held in memory |
| `LIVESYNC_BACKEND` | none | Required | Select `couchdb` or `s3` |
| `COUCHDB_CUSTOM_HEADERS` | empty | Optional | Plugin Custom Headers lines or JSON object; credential values; see [connection examples](connecting-storage.md#reverse-proxies-and-auth-proxies) |
| `COUCHDB_CONNECT_TIMEOUT_SECONDS` | `5` | Optional | Positive finite seconds for CouchDB connection |
| `COUCHDB_IO_TIMEOUT_SECONDS` | `30` | Optional | Positive finite seconds for CouchDB read, write and pool |
| `S3_CONNECT_TIMEOUT_SECONDS` | `5` | Optional | Positive finite seconds for S3 connection |
| `S3_READ_TIMEOUT_SECONDS` | `30` | Optional | Positive finite seconds for S3 response reads |
| `S3_MAX_ATTEMPTS` | `1` | Optional | Positive integer SDK attempts for reads and conditional writes; unconditional PUT always one |
| `LIVESYNC_FRONTMATTER_MAX_NODES` | `20000` | Optional | Positive integer expanded YAML nodes for properties/tags only |
| `LIVESYNC_FRONTMATTER_MAX_BYTES` | `4194304` | Optional | Positive integer expanded UTF-8 string/key bytes for properties/tags only |
| `LIVESYNC_FRONTMATTER_MAX_DEPTH` | `200` | Optional | Positive integer property nesting depth; parser runtime ceiling also applies |
| `LIVESYNC_SELECTION_TIMEOUT_SECONDS` | `5` | Optional | Positive finite seconds for Node capability checks and selection requests |
| `COUCHDB_URL` | `http://localhost:5984` | Optional | CouchDB server base URL |
| `COUCHDB_USER` | none | Required for CouchDB Basic auth; absent for JWT | Database username |
| `COUCHDB_PASSWORD` | none | Required for CouchDB Basic auth; absent for JWT | Database password |
| `COUCHDB_DATABASE` | none | Required for CouchDB | LiveSync database name |
| `COUCHDB_JWT_ALGORITHM` | empty (Basic auth) | Select JWT mode | HS256, HS512, ES256 or ES512; remove COUCHDB_USER and COUCHDB_PASSWORD |
| `COUCHDB_JWT_KEY` | none | Required for JWT | Base64 HMAC key or matching PKCS8 EC PEM private key; never reported |
| `COUCHDB_JWT_SUBJECT` | none | Required for JWT | Plugin JWT Subject (sub), nonblank; original whitespace preserved |
| `COUCHDB_JWT_KEY_ID` | empty (omit kid) | Optional for JWT | Plugin JWT Key ID (kid); match server key entry; ES512 acceptance uses a named kid |
| `COUCHDB_JWT_EXPIRY_MINUTES` | 5 | Optional for JWT | Nonnegative finite minutes; 0 uses the plugin five-minute fallback; explicit empty is refused; cached token refreshes when exp is strictly less than integer current seconds + 10 |
| `LIVESYNC_S3_URI` | empty | Required unless S3 fields set | `sls://access:secret@endpoint?bucket=…&region=…&prefix=…` connection |
| `S3_ENDPOINT_URL` | none | Required for S3 fields | S3-compatible endpoint URL |
| `S3_ACCESS_KEY_ID` | none | Required for S3 fields | Object-storage access key |
| `S3_SECRET_ACCESS_KEY` | none | Required for S3 fields | Object-storage secret key |
| `S3_BUCKET` | none | Required for S3 fields | Existing LiveSync journal bucket |
| `S3_REGION` | `auto` (fields); empty if omitted in URI | Optional | Object-storage region |
| `S3_CUSTOM_HEADERS` | empty | Optional | Plugin Custom Headers lines or JSON object, safe fields applied after signing on every S3 request; transport-owned fields refused |
| `S3_FORCE_PATH_STYLE` | `true` | Optional | `true` path style, `false` virtual-host style for configured endpoints |
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
| `LIVESYNC_SCAN_MAX_NOTES` | `0` | Nonnegative integer | Aggregate note cap; 0 unlimited |
| `LIVESYNC_SCAN_MAX_BYTES` | `0` | Nonnegative integer | Aggregate UTF-8 input byte cap; 0 unlimited |
| `LIVESYNC_SCAN_MAX_SECONDS` | `20` | Nonnegative finite number | Aggregate scan and index time budget including scan-slot waiting; 0 unlimited |
| `LIVESYNC_TEXT_SCAN_MAX_BYTES` | `empty` | Nonnegative integer or empty | Whole-note UTF-8 text access budget; empty resolves to max(positive vault size policy, 256 MiB); 0 unlimited |
| `LIVESYNC_REGEX_MAX_OUTPUT_BYTES` | `0` | Nonnegative integer | Regex replacement output bytes; 0 uses effective text access budget |
| `LIVESYNC_REGEX_TIMEOUT_SECONDS` | `10` | Positive finite number | Persistent isolated regex worker request deadline in seconds |
| `LIVESYNC_TEXT_VERDICT_CACHE_ENTRIES` | `65536` | Positive integer | Negative verdict LRU bound |
| `LIVESYNC_SYNC_ONLY_REGEX` | `empty` | Optional | Local allow regex list, literal `|[]|` separators |
| `LIVESYNC_SYNC_IGNORE_REGEX` | `empty` | Optional | Local ignore regex list |
| `LIVESYNC_USE_IGNORE_FILES` | `empty` | Optional boolean | Empty/unset derives useIgnoreFiles from vault settings; true/false overrides |
| `LIVESYNC_IGNORE_FILES` | `empty` | Optional | Empty/unset derives ignoreFiles; non-empty uses JS-trimmed comma names including empty entries; unresolved explicit names warn |
| `LIVESYNC_SYNC_MAX_SIZE_MB` | `empty` | Finite number or empty | Strict operator override; vault data uses iterative JavaScript numeric coercion; <=0 disables |
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

## HTTP request bodies

`LIVESYNC_MAX_REQUEST_BYTES` defaults to 1 GiB. A base64 upload at the default
50 MiB vault policy needs about 68 MiB plus JSON overhead; raised vault policies
need proportionally more. The body is held in memory. Raise this variable or
set 0 (unlimited within Python addressable memory) and restart after HTTP 413.
Reverse proxies may have their own body limits. The OAuth authorization form
has its separate 4 MiB cap; changing this setting does not change that cap.

For reproducible development/test installation use a fresh Python 3.13 virtual
environment and `python -m pip install -r requirements.lock`; this frozen file
is produced by `./venv/bin/python -m pip freeze` of the working acceptance venv.
