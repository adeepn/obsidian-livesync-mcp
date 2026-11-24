# Deployment, clients and authentication

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
