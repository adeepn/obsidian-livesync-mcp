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

**S3 individual fields:** use these or the alternative URI block below; copy the values
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

## CouchDB JWT authentication

Use the [JWT field recipe and server provisioning guide](couchdb-jwt.md).
Basic and JWT are exclusive; MCP authentication is separate.

## Alternative connection forms

The audited plugin's **Copy the current settings to a Setup URI** (often called
“Copy setup URI”) produces an encrypted settings link, not this peer's S3 URI.
Do not paste it into `LIVESYNC_S3_URI`: the peer does not decrypt setup exports.
Use the individual fields above. Source: commonlib at the pin,
`serviceFeatures/setupObsidian/setupUri.ts:19–30` and
`API/processSetting.ts:218–255`.

If a compatible connection URI is already available, the accepted form is
`sls://access:secret@endpoint?bucket=…&region=…&prefix=…`; paste it as one line:

```dotenv
LIVESYNC_BACKEND=s3
LIVESYNC_S3_URI=<paste-sls://-setup-URI>
```

For custom headers, proxies and addressing, see the sections below.
For disposable developer server tests, see [automated real-server acceptance](developer-acceptance.md#automated-real-server-acceptance).

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
at the configured origin. Subject to the transport-owned exclusions below,
like normal device replication
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

## Transport policy

COUCHDB_CONNECT_TIMEOUT_SECONDS (5) controls connection establishment;
COUCHDB_IO_TIMEOUT_SECONDS (30) controls read, write and pool waits.
S3_CONNECT_TIMEOUT_SECONDS (5) and S3_READ_TIMEOUT_SECONDS (30) control the SDK.
All accept positive finite seconds. Raise them and restart for a healthy slow
server. S3_MAX_ATTEMPTS is a positive integer (default 1); see the
[S3 retry policy](s3-journal.md#transport-policy) before raising it. A timeout
after mutation dispatch can mean outcome unknown: re-read before retrying.

Custom headers deliberately differ from the plugin for transport safety:
Content-Length, Transfer-Encoding, Host, Connection, Keep-Alive, TE, Trailer,
Upgrade and all Proxy-* fields are transport-owned and refused case-insensitively
for both storages. Remove these entries and configure routing/proxy transport
separately. Other headers still apply after S3 signing.
