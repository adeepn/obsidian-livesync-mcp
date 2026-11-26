# CouchDB JWT authentication



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
name separate, and add the [shared MCP authentication block](connecting-storage.md):

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


Custom headers retain the peer's transport-safety difference from the plugin:
syntactically valid transport-owned Host, Content-Length, Transfer-Encoding,
Connection, Keep-Alive, TE, Trailer, Upgrade and all Proxy-* fields are refused
with value-free configuration errors. Remove them and configure routing/proxy
transport separately. This restriction also applies with JWT authentication;
see [custom header policy](connecting-storage.md#reverse-proxies-and-auth-proxies).
