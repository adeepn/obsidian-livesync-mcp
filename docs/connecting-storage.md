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

Open **Settings → Self-hosted LiveSync → Setup / remote configuration →
CouchDB Configuration** (wording varies by installed version). Read **URL**,
**Username**, **Password**, **Database Name**. Existing settings may appear in
the remote-configuration page instead of the wizard; do not create a new DB.
The peer uses username/password; the plugin's optional JWT authentication is
not implemented here.

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

Open **Settings → Self-hosted LiveSync → Setup / remote configuration →
S3/MinIO/R2 Configuration**. Copy **Endpoint URL**, **Access Key ID**,
**Secret Access Key**, **Bucket Name**, **Region**, **Folder Prefix**. Check
**Use Path-Style Access**: this peer always uses path-style for a configured
endpoint (`object_storage.py`); there is no path-style env switch. A provider
requiring virtual-host addressing is not supported by this adapter.

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
