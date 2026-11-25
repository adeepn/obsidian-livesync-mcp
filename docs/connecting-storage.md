# Connect an existing LiveSync vault

### Quick start matrix

Open **Obsidian → Settings → Self-hosted LiveSync** in a vault that already
syncs successfully. Use the connection settings for that same vault, not a new
bucket or database. Labels can vary with plugin version.

| Backend | Where credentials come from | Configuration | First tool call | Smoke check |
| --- | --- | --- | --- | --- |
| CouchDB | Plugin remote database settings: CouchDB URI, username, password and database name | CouchDB block below + shared server/auth block | Direct access, no full journal replay or local replica | Call `obsidian_vault_stats`, then `obsidian_list_notes` from an MCP client |
| S3 journal | Plugin object-storage/journal settings: endpoint, access key, secret key, bucket, region and prefix; supported `sls://…` connection URI if already available | Exactly one S3 block below + shared server/auth block | Full journal replay; time scales with history and network, disk roughly vault size or larger with history | Call the same two tools; allow replay to finish or continue across bounded refreshes |

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
[clients and authentication](clients-and-authentication.md). Alternatively use the complete OAuth
group instead of `MCP_API_KEY`.

## CouchDB

For a vault that already syncs, open **Settings → Self-hosted LiveSync → Remote
Configuration → Connection settings → Saved connections**. Find the row marked
**(Active)** and click its wrench (**Configure**). In **CouchDB Configuration**,
read **URL**, **Username**, **Password**, and **Database Name**. Expand
**Advanced Settings** for **Custom Headers**, or **Experimental Settings** for
JWT fields, if used.
After reading, click **Cancel**. Do not save changes, switch connections or create
a database merely to connect the peer.

<!-- Pinned plugin 5070b67: src/modules/features/SettingDialogue/SettingsPageCatalogue.ts:137-143;
PaneRemoteConfig.ts:187-188,389-417; SetupWizard/dialogs/SetupRemoteCouchDB.svelte:170-236,333. -->
Choose the Basic block below or the [JWT route](#couchdb-jwt-authentication).
These authenticate the peer to CouchDB; MCP client authentication is separate.

Copy those four values into this block.
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

## CouchDB JWT authentication

In the pinned plugin's **CouchDB Configuration → Experimental Settings**, enable
**Use JWT Authentication**. Match these dialog labels exactly (plugin
`src/modules/features/SetupWizard/dialogs/SetupRemoteCouchDB.svelte:240-297`):

| Plugin dialog label | Peer variable | Where the value comes from |
| --- | --- | --- |
| JWT Algorithm | `COUCHDB_JWT_ALGORITHM` | Same selection: HS256, HS512, ES256 or ES512 |
| JWT Key | `COUCHDB_JWT_KEY` | Same private signing material as the device: base64 HMAC bytes, or PKCS8 EC private PEM (P-256 for ES256, P-521 for ES512) |
| JWT Subject (sub) | `COUCHDB_JWT_SUBJECT` | Same nonblank CouchDB username/subject as the device |
| JWT Key ID (kid) | `COUCHDB_JWT_KEY_ID` | Same server key identifier; empty omits kid and selects the server default |
| JWT Expiration Duration (minutes) | `COUCHDB_JWT_EXPIRY_MINUTES` | Same nonnegative finite duration; omit or use 0 for 5 minutes; negatives refuse because upstream would mint already-expired tokens |

Startup explicitly checks `GET /_session`: `userCtx.name` must equal
`COUCHDB_JWT_SUBJECT`. Anonymous or different subjects fail with typed
authorization guidance naming key, algorithm, key ID, subject and clock/expiry.
Later HTTP 401 is an authorization error; a later HTTP 400 probes `/_session`
with the same token so ordinary malformed requests are not misclassified.
Remote reason text is never used in diagnostics, even with unsafe debug enabled.

Remove `COUCHDB_USER` and `COUCHDB_PASSWORD` entirely, including empty definitions.
Do not combine the Basic block with this alternative. Keep the URL and database
name separate, and add the shared MCP authentication block above:

```dotenv
LIVESYNC_BACKEND=couchdb
COUCHDB_URL=<CouchDB-server-URI>
COUCHDB_DATABASE=<plugin-database-name>
COUCHDB_JWT_ALGORITHM=HS256
COUCHDB_JWT_KEY=<plugin-jwt-key>
COUCHDB_JWT_SUBJECT=<plugin-username>
COUCHDB_JWT_KEY_ID=
COUCHDB_JWT_EXPIRY_MINUTES=5
```

Replace every angle-bracket field locally. For ES256/ES512, change the algorithm
and quote the entire PKCS8 PEM key as a multiline dotenv value; python-dotenv also
expands literal `\n` inside double quotes. Never paste signing keys into logs,
issues or MCP calls. A signing key is not a pre-generated bearer token.
The plugin imports ES keys with `crypto.subtle.importKey("pkcs8", …)` after
stripping PEM delimiters and whitespace (`httplib.ts:84-101`). SEC1
`EC PRIVATE KEY` is rejected. Generate PKCS8 for ES256 with:

```bash
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt
```

For ES512, use `secp521r1` instead of `prime256v1`. Keep generated private keys
local; install only the matching public key on CouchDB.

The server administrator must enable the JWT handler on every CouchDB node.
For CouchDB 3.5, the handler list is `[chttpd] authentication_handlers = cookie,
jwt, default`. Restart CouchDB after changing the handler list. In `[jwt_keys]`,
configure `hmac:_default` with the same base64 shared key for HS256/HS512;
configure `ec:_default` with the matching **public** SPKI PEM for ES256/ES512
(newlines escaped as `\n` in the INI value). If kid is nonempty, replace
`_default` with that exact ID, for example `ec:es512`. Keep the private EC key
only on signing clients. Preserve the default `_couchdb.roles` claim mapping:
the plugin and peer both send `["_admin"]`; configure this only for trusted admin
signers. `sub` is mandatory. These server requirements are described in the
[official CouchDB JWT documentation](https://docs.couchdb.org/en/stable/api/server/authn.html#jwt-authentication).
The handler restart requirement was reported by the controller on 3.5.3; no
live server is required to generate tokens.

The peer sends `Authorization: Bearer <token>` on every CouchDB request, caches
one token per backend and refreshes at `exp < integer current seconds + 10`
(strict inequality). HS tokens are byte-identical at the same second; EC
signatures vary but carry identical header/payload bytes. The pinned plugin
validates a trimmed subject but signs the original subject, including whitespace.
Its `btoa` JSON encoder only accepts Latin-1 identifiers; use matching Latin-1
subject/kid values on the server and device if validation refuses them. Tokens
include the plugin's `_admin` role, not an independently configurable role list.
`backend_info` and `obsidian_vault_stats` expose only `auth_mode` (`basic` or
`jwt`) and `jwt_algorithm`; no key, subject, kid or token is reported. Config
errors name the variables to correct; wrong server verification keys or claims
produce sanitized CouchDB errors. Check the handler, key family, kid, subject,
clock and required claims against the device settings before retrying.

## S3 journal

Open **Settings → Self-hosted LiveSync → Remote Configuration → Connection
settings → Saved connections → (Active) row → Configure** (wrench).
In **S3/MinIO/R2 Configuration**, copy **Endpoint URL**, **Access Key ID**,
**Secret Access Key**, **Bucket Name**, **Region**, and **Folder Prefix**.
Expand **Advanced Settings** for **Custom Headers**, if used. Click **Cancel**
after reading the fields.

<!-- Pinned plugin 5070b67: SettingDialogue/PaneRemoteConfig.ts:187-188,389-417;
SetupWizard/dialogs/SetupRemoteBucket.svelte:143-235,257. -->

Check **Use Path-Style Access**: copy this choice with `S3_FORCE_PATH_STYLE=true`
(the default) or `S3_FORCE_PATH_STYLE=false` for virtual-host addressing.
Virtual-host-only providers require false. DNS and the TLS certificate must
cover `bucket.endpoint-host`; an IP endpoint or a bucket name unsuitable for
DNS may cause the SDK to fall back to path style. Use a DNS-compatible bucket
and hostname for virtual-host access. Like upstream, the switch applies only
when an endpoint is configured; without one, AWS SDK endpoint rules apply.

The audited plugin's **Copy the current settings to a Setup URI** (often called
“Copy setup URI”) produces an encrypted settings link, not this peer's S3 URI.
Do not paste it into `LIVESYNC_S3_URI`: the peer does not decrypt setup exports.
Use the individual fields below. Source: commonlib at the pin,
`serviceFeatures/setupObsidian/setupUri.ts:19–30` and
`API/processSetting.ts:218–255`.

If a compatible connection URI is already available, the accepted form is
`sls://access:secret@endpoint?bucket=…&region=…&prefix=…`; paste it as one line:

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

Copy the prefix **exactly**, including any trailing slash: `folder/` stores
objects below that prefix, while `folder` is a different literal prefix. Do not
add or remove `/` from an existing connection. Empty prefix means the bucket
root. Source: commonlib `replication/journal/objectstore/MinioStorageAdapter.ts:137,163,181,226-233`;
peer `object_storage.py:124-125,171`.
Use the plugin's region and prefix if nonempty. Optional local state settings
are described in [local state and recovery](local-state-and-recovery.md). Do not leave both S3 connection forms enabled,
even with empty individual values. If starting from `.env.example`, remove its
unused backend/auth settings and replace its placeholder secrets.

Start with `python run.py`, connect an MCP client to `http://localhost:8484/mcp`
using the bearer token, and call `obsidian_vault_stats` and `obsidian_list_notes`.
Expect the chosen `backend`, sensible note counts and known vault paths.
CouchDB access is immediate apart from the requested vault scan; S3's initial
replay can be lengthy. Startup validates settings but does not replay journals.
There is no dedicated health endpoint; these tool calls check storage access too.

## First access and smoke check

Existing process environment variables take precedence over `.env` (`run.py`
calls python-dotenv with its default `override=False`). The systemd unit uses
`EnvironmentFile=`; the interactive shell does not load `.env` automatically.

1. Save `.env` locally, with shared auth plus exactly one backend block.
2. Start `./venv/bin/python run.py` from the repository root.
3. Add the `/mcp` streamable HTTP URL and bearer token to the MCP client.
4. Call `obsidian_vault_stats`; check `backend`, counts and backend diagnostics.
5. Call `obsidian_list_notes`; confirm a path already visible in Obsidian.

CouchDB has direct access; it does not make a full local replica. S3 starts a
full journal replay on first access. A photo-heavy vault may contain gigabytes
of journal history: allow minutes or longer, roughly vault-sized disk space
initially and potentially much more for historical revisions. There is no
fixed time/disk multiplier; throughput, history and budgets dominate. As a
transfer-only illustration, 10 GiB at 20 MiB/s takes about 8.5 minutes before
parsing/SQLite overhead. Completed replay progress survives a budget stop;
raise the named budget or continue refreshes after the throttle interval.
See [S3 operation](s3-journal.md) and [recovery](local-state-and-recovery.md).

The offline documentation tests substitute marked angle-bracket fields with
local disposable values, parse every block and call `config.load_config`.
That proves configuration syntax/validation, not connectivity to a real store.

### Selection policy

Ignore-file settings derive from vault PREFERRED, or agreeing device maps, unless
LIVESYNC_USE_IGNORE_FILES (true/false) and LIVESYNC_IGNORE_FILES (non-empty names)
independently override them. Unset/empty means derive. A vault with
useIgnoreFiles=true requires Node.js 22+ in the service PATH. Explicit false is
allowed, but devices drop peer writes to ignored paths. Regex lists stay env-only
because syncOnlyRegEx/syncIgnoreRegEx are absent from TweakValuesTemplate.
Reads bypass all filters, including explicit filters; the database remains the
source of truth even when devices keep local files that later become ignored.
See [selection filters](selection-filters.md) for syntax and diagnostics.


## Reverse proxies and auth proxies

Copy **Custom Headers** from the CouchDB or S3/MinIO/R2 setup dialog into
`COUCHDB_CUSTOM_HEADERS` or `S3_CUSTOM_HEADERS`, respectively. The exact labels
at the pin are in `SetupRemoteCouchDB.svelte:229-234` and
`SetupRemoteBucket.svelte:230-235`. These are local connection credentials;
they are independent of the shared vault settings and of MCP client auth.
Keep them in a protected environment file or launcher secret store.

For an HTTPS reverse proxy, use the server base URL including its optional
path prefix; the database remains a separate field. Example (replace marked
fields, including the proxy credential copied locally from **Custom Headers**):

```dotenv
LIVESYNC_BACKEND=couchdb
COUCHDB_URL=https://vault.example.org/livesync/
COUCHDB_USER=<plugin-username>
COUCHDB_PASSWORD=<plugin-password>
COUCHDB_DATABASE=<plugin-database-name>
COUCHDB_CUSTOM_HEADERS="X-Proxy-Token: <plugin-custom-header-value>\nX-Client: obsidian-peer"
```

For a direct IP connection, this alternative block keeps the database separate
from the endpoint (replace the credentials and database locally):

```dotenv
LIVESYNC_BACKEND=couchdb
COUCHDB_URL=http://127.0.0.1:5984
COUCHDB_USER=<plugin-username>
COUCHDB_PASSWORD=<plugin-password>
COUCHDB_DATABASE=<plugin-database-name>
```

Use the actual server IP instead of loopback when the peer runs elsewhere.
For a direct IP endpoint use, for example, `COUCHDB_URL=http://127.0.0.1:5984`
or `S3_ENDPOINT_URL=http://127.0.0.1:9000`; retain the port and optional proxy
path prefix. For HTTPS use a certificate valid for that IP, or use its valid
DNS hostname. The CouchDB peer does not follow redirects; configure the final
server URL so credentials cannot be forwarded to another host.

In a `.env` file, python-dotenv expands `\n` inside double quotes into real
newlines. Literal shell or systemd environment values do not necessarily do
this. For those launchers use a JSON object of string to string; outer single
quotes below belong to dotenv, not to the value passed to the process:

```dotenv
LIVESYNC_BACKEND=s3
S3_ENDPOINT_URL=https://objects.example.org
S3_ACCESS_KEY_ID=<plugin-access-key>
S3_SECRET_ACCESS_KEY=<plugin-secret-key>
S3_BUCKET=<plugin-bucket>
S3_REGION=auto
S3_CUSTOM_HEADERS='{"X-Proxy-Token":"<plugin-custom-header-value>","X-Client":"obsidian-peer"}'
S3_FORCE_PATH_STYLE=false
```

Either variable accepts either syntax, independently. Empty means no custom
headers. Text parsing follows commonlib `common/utils.ts:459-469`: split on
newlines, trim key/value, ignore lines with either empty, last duplicate
**exact-case** key wins. Upstream splits at most two colon segments and
therefore `X-Value: first:second` becomes `first`; use JSON to retain colons.
JSON strings retain their whitespace; duplicate JSON keys are rejected.
Invalid JSON, non-string JSON values, invalid RFC 9110 token names and control
characters in resulting values fail startup. The error names only the variable,
entry ordinal (line number in text, member number in JSON) and broken rule;
correct that entry. No header name or credential value is echoed.

CouchDB sends headers on metadata, document, attachment and `_local` requests
at the configured origin. Like normal device replication
(`RemoteService.ts:360-369`), custom headers
append to existing headers, including Authorization and Content-Type. An
Authorization entry therefore combines with Basic auth; use the proxy's own
header name when it expects a separate token. S3 applies headers to every final
signed wire request,
including conditional publication and the exists+put fallback. S3 headers can
override SDK headers, including Authorization; use this only when the proxy
expects it. Incorrect overrides can invalidate an AWS signature: remove the
override or configure the proxy's required header instead.

`backend_info` and `obsidian_vault_stats` expose only `custom_headers_configured`
(boolean) and `custom_headers_count` (integer). They reveal no header names or
values. SDK/HTTP library logging is suppressed process-wide after a connection
with custom headers is constructed, including debug tracebacks; typed application
diagnostics remain available. HTTP header values use Latin-1 octets, like the
device Fetch ByteString
contract. Code points above U+00FF cannot be sent as such HTTP field values;
encode those proxy credentials in ASCII (for example the proxy's documented
base64 token format). These offline tests establish configuration and wire behavior,
not connectivity to a particular reverse proxy.

JWT expiry input is numeric: an explicit empty `COUCHDB_JWT_EXPIRY_MINUTES` is refused; omit it or use `0` for the five-minute fallback. The upstream literal JSON string `"0"` is truthy and gives a zero-second token; this numeric environment variable cannot represent that string. For numeric 0 and 5, all four algorithms retain the cached token through +290.99 seconds and refresh at +291 seconds (httplib.ts:166-167,181-196).

## Automated real-server acceptance

The env-gated tests use disposable S3 prefixes and `mcp_test_<random>` CouchDB
databases. Supply `MINIO_TEST_ENDPOINT`, `MINIO_TEST_KEY`, `MINIO_TEST_SECRET`,
`MINIO_TEST_BUCKET`, `COUCHDB_TEST_URL`, `COUCHDB_TEST_USER` and
`COUCHDB_TEST_PASSWORD` only in the invoking shell. No variables means skips
and no server access. Never point these tests at an owner's vault.

`tests/test_integration_parity_s3.py` and
`tests/test_integration_parity_couchdb.py` reuse the 60-row pairwise settings
selection and committed published-dependency oracle. `MINIO_TEST_MATRIX=full`
also includes the existing supplemental branch rows on both backends. The
matrix lifecycle retains each row's document kind and additionally checks plain
text and binary newnote documents in every matrix row, with exact native-oracle
children and fresh-reader comparisons.

JWT tests additionally require `COUCHDB_TEST_JWT_URL`,
`COUCHDB_TEST_JWT_SUBJECT`, and `COUCHDB_TEST_JWT_HS_KEY` or
`COUCHDB_TEST_JWT_ES_KEY`; ES512 uses `COUCHDB_TEST_JWT_ES512_KEY` and
`COUCHDB_TEST_JWT_KEY_ID_ES512=es512`. EC keys must be PKCS8 PEM. All four
algorithms (HS256, HS512, ES256, ES512), wrong-key and expired-clock refusals
are exercised. Backend construction
uses JWT configuration without Basic credentials. Test environment values are
never copied into `.env` or committed artifacts.

```bash
./venv/bin/python -m pytest tests/test_integration_minio.py tests/test_integration_couchdb.py tests/test_integration_parity_s3.py tests/test_integration_parity_couchdb.py -q --tb=short
```

The final F4 run closes JWT wrong-key HTTP 400 classification and confirms
CouchDB rejects device-shaped lone-surrogate JSON while the peer refuses it
before sending any request. The rotated EC test keys use PKCS8.

Acceptance also exercises 40 MiB and 100 MiB binary uploads under a 200 MiB
vault policy, byte-exact fresh-peer reads, exact native-oracle child IDs and
piece digests, a 201 MiB skip without publication, and an oversized rename
with its source intact. A 12 MiB textual `.json` newnote traverses `create_app`
MCP create, whole/window fetch, near-end search, append and search-replace.
Recovery checks retained local revisions/chunks after wipe and epoch changes,
identity quarantine with remote rebuild, and interrupted replay checkpoints.
S3 before-send events capture custom headers on authenticated outgoing requests;
CouchDB request hooks cover GET, POST and PUT, including local documents,
listing and leaf/metadata publication. Real compression reads include committed
upstream vectors with the peer write setting both false and true.

Large-file oracle regeneration (offline; Node 26.13.1+, exact published archives):

```bash
node scripts/extract_acceptance_vectors.mjs COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TGZ WASM_TGZ tests/fixtures/acceptance_upstream.json
```

See [the final acceptance artifact](test-artifacts/2026-11-25-parity-docker/summary.txt)
for counts, durations, input digests, image digests and reproduction commands.
This establishes the enumerated scenarios on these servers; it does not prove
all possible settings or server combinations.


## Connection constraints

Encrypted `obsidian://setuplivesync?settings=…` exports and native `sls+s3` saved
connection strings are not accepted as `LIVESYNC_S3_URI`. This parser accepts its
literal documented `sls://` form only and does not percent-decode credentials;
individual fields avoid delimiter ambiguity. Keep all fields, headers and keys
local. E2EE/path-obfuscated vaults are unsupported (#3/#4); use an unencrypted,
unobfuscated test vault rather than disabling encryption on a working vault.
HTTP redirects are deliberately not followed for CouchDB; configure the final
origin/base path to avoid forwarding credentials. Custom header HTTP validity,
Latin-1 transport requirements, JWT key formats and addressing-style DNS/TLS
constraints above are real protocol/SDK requirements. Fixed transport deadlines
and retries are listed in [the limit inventory](peer-limitations.md#operational-and-tool-bounds).
