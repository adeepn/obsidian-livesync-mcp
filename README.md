# obsidian-livesync-mcp

Remote MCP server for accessing your Obsidian vault via CouchDB / [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync). Works with Claude Code, Claude.ai, and any MCP-compatible client.

No Obsidian desktop app required on the server — connects directly to CouchDB.

## Architecture

```
Obsidian (phone/desktop) <-> CouchDB <-> MCP Server <-> Claude Code / Claude.ai
```

Obsidian clients sync via the LiveSync plugin to a CouchDB instance. This MCP server reads and writes directly to CouchDB, providing 20 tools for managing your vault — no Obsidian REST API or desktop app needed on the server side.

## Backends

Storage is selected by a single environment variable, `LIVESYNC_BACKEND`:

- **`couchdb`** (current) — talks directly to the CouchDB instance your vault syncs to. Configure it with the `COUCHDB_*` variables below.
- **`s3`** (Plan B, not yet available) — will support Self-hosted LiveSync setups that keep the vault in S3-compatible storage. Selecting it today fails at startup with an explicit message.

All tools work through a storage-agnostic backend interface, so switching backends later is a configuration change, not a rewrite. The `vault_stats` tool reports the active backend in its `backend` field.

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
- **vault_stats** — Vault statistics
- **create_from_template** — Create from template with variable substitution
- **list_templates** — List available templates

## Prerequisites

- CouchDB with Obsidian LiveSync data
- Python 3.11+

### Vault compatibility

The server derives document ids from vault paths the same way LiveSync does, so it supports vaults using the plugin's default path settings:

- **No end-to-end encryption** — E2EE vaults are rejected at startup (and encrypted chunks are refused on read).
- **No path obfuscation** — obfuscated vaults are rejected at startup.
- **Case-insensitive filenames** (`handleFilenameCaseSensitive` off, the default) — document ids are lowercased paths.
- **No Unicode NFC path normalization** (`pathNormalizationVersion` 0, the default).

A vault using an unsupported combination fails the startup check with an explicit error instead of serving mismatched data.

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

| Variable | Description | Default |
|---|---|---|
| `LIVESYNC_BACKEND` | Storage backend: `couchdb` or `s3` (Plan B, not yet available) | required, no default |
| `COUCHDB_URL` | CouchDB URL | `http://localhost:5984` |
| `COUCHDB_USER` | CouchDB username | required when backend is `couchdb` |
| `COUCHDB_PASSWORD` | CouchDB password | required when backend is `couchdb` |
| `COUCHDB_DATABASE` | LiveSync database name | required when backend is `couchdb` |
| `MCP_API_KEY` | Shared bearer token for MCP clients such as Codex (bearer mode) | required unless OAuth group is set |
| `OAUTH_PASSWORD` | Password enforced at the `/authorize` form in OAuth mode | required for OAuth, together with the two below |
| `OAUTH_CLIENT_ID` | OAuth client ID for pre-registered client | required for OAuth |
| `OAUTH_CLIENT_SECRET` | OAuth client secret | required for OAuth |
| `MCP_SERVER_URL` | Public HTTPS URL of this server | `https://localhost:8484` |
| `MCP_HOST` | Bind address | `0.0.0.0` |
| `MCP_PORT` | Port | `8484` |

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
The future Plan B journal backend must document its own scan semantics and
protect chunks referenced by active scans from reclamation.
