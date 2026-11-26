# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Settings parity

- Classify unreadable CouchDB PUT/POST/DELETE acknowledgements as outcome unknown
  with typed budget/encoding recovery and non-idempotent retry guidance. Preserve
  read refusals, uncertain orphan-leaf handling and move source-removal context.

- Match source-audited ordinary-file identities and writes at plugin 1.0.52 /
  commonlib 484c164: legacy/NFC and case-sensitive/insensitive paths, all V1/V2/
  V2-segmenter/V3 splitters, legacy migration, five chunk hash modes and Eden.
  Preserve JavaScript scalar coercion, UTF-16 lengths, Unicode boundaries,
  duplicate child order and empty tails. Refuse proven native loops, ambiguous
  chunk collisions and lossy binary parameter combinations with recovery guidance.
- Follow device disk-ingestion suffixes for text and binary content. Add binary
  attachment upload/download and editing of strictly valid UTF-8 textual newnotes.
  Admit files under vault syncMaxSizeInMB (default 50 MiB, equality admitted,
  <=0 unlimited), with structured skipped writes and readable oversized revisions.
  Invalid plain-path UTF-8 requires explicit lossy conversion.
- Derive useIgnoreFiles/ignoreFiles, size and text policy from vault settings,
  retaining independent local overrides. Keep syncOnlyRegEx/syncIgnoreRegEx as
  operator environment settings; they are not published in the milestone.
  Active selection uses verified
  pinned ECMAScript/minimatch code and requires Node 22+; inactive selection needs
  no Node. Refresh journal policies atomically and refuse incompatible active rules.
- Support CouchDB Basic/JWT authentication (HS256/HS512/ES256/ES512), per-backend
  token caching, startup session-identity checks and PKCS8 EC keys. Add CouchDB/S3
  custom headers for auth proxies and configurable S3 path/virtual-host addressing.
  For transport safety, refuse Content-Length, Transfer-Encoding, Host, hop-by-hop
  and Proxy-* overrides, including injected configurations.
- Read compressed CouchDB documents regardless of the write setting. Enable
  shorter-only compression under enableCompression while preserving CR/LF and BOM
  bytes. Bound inflation before read projection or reserved-prefix publication;
  LIVESYNC_COUCHDB_MAX_INFLATED_BYTES overrides a splitter-derived 32 MiB floor.
  Force HTTP identity encoding and bound streamed wire/decoded response bytes
  before JSON parsing; refuse unsupported encodings with recovery guidance.
- Use device JSON/TextEncoder wire encodings and retain original chunk IDs.
  Report BOM removal, invalid UTF-8/surrogate replacement, reserved compression
  prefix interpretation and journal transport_replacements through typed notices
  without rewriting the original tool result. Refuse CouchDB lone-surrogate
  publications before sending leaves because the real server rejects them.
- Complete aggregate scans and resumable CouchDB indexing by default, with unlimited
  note/byte scan defaults, configurable time budgets, cursors, page limits and fetch
  line windows. Share a whole-note text budget (max of positive vault policy and
  256 MiB; 0 unlimited override) across text tools and scans; complete byte downloads
  remain available. Isolate regex execution with configurable deadlines/output bounds.
- Resolve YAML aliases in properties/tags, preserve untouched key bytes during edits,
  and bound expanded nodes (20000), UTF-8 key/string bytes (4 MiB) and depth (200)
  through named environment overrides; raw-note access remains available.
- Make HTTP request-body admission explicit: LIVESYNC_MAX_REQUEST_BYTES defaults
  to 1 GiB, 0 unlimited. Bodies are held in memory; HTTP 413 requires raising the
  policy or any proxy limit. The OAuth authorization form retains its separate cap.
  Make storage and selection deadlines and safe S3 retry counts configurable.
- Retain revision history, historical/shared chunks and local-only survivors across
  wipe/epoch recovery. Quarantine state on identity changes and report state size.
  Confirm ambiguous conditional publication at the original key; retain outcome
  uncertainty when read-back cannot prove success. Protect rename sources when
  destinations are excluded or oversized; report partial moves and safe retry steps.
- Expose only typed diagnostic fields in configuration, tools and hosting logs;
  withhold remote values, headers, exception text and tracebacks. Verify installed
  selection module/worker hashes before execution. Freeze the tested environment in
  requirements.lock and pin the upstream watcher runner to Ubuntu 24.04.
- Deliberate differences remain: E2EE/path obfuscation are unsupported (#3/#4);
  filesystem reflection and internal/plugin sync are outside ordinary-tool scope;
  conflict automation and metadata deletion remain conservative. The peer never
  registers as a device or publishes tweak alignment. Operational replay, inflate,
  scan and property budgets are distinct from device file admission and name overrides.
- Persist each peer's S3_STATE_DIR and back it up before upgrades. Restart after
  configuration changes and align incompatible device tweaks. Use the focused
  configuration, differences, recovery and oracle-regeneration references. After a
  confirmed disposable-server wipe, ordinary sync creates service records; rebuilding
  from device files is the explicit recovery step before checking files.

### Earlier unreleased infrastructure

- Add storage-agnostic CouchDB/S3 backends, strict startup configuration,
  LIVESYNC_BACKEND, bearer auth and backend diagnostics. Default CouchDB URL to
  http://localhost:5984 and provide frozen dependency installation.
- Enforce create/overwrite, optimistic locking, soft-delete visibility and safe
  chunk publication; reject missing databases, malformed canvas and unsafe writes.
- Use round-trip YAML frontmatter and Obsidian-compatible links/graph resolution.
  Enforce OAuth password authorization with PKCE and preconfigured Claude callbacks;
  keep dynamic client registration disabled and document Codex bearer/HTTP setup.

## [1.0.1] - 2026-04-10

### Fixed

- `read_note_content` now raises `ValueError` on missing or malformed CouchDB chunks instead of silently skipping them — this was causing sections to disappear after search-replace on large documents
- `write_note` now creates the new chunk before deleting old ones so a mid-write failure cannot permanently corrupt the document
- `obsidian_update_note` catches `ValueError` from the read stage and returns an explicit error message instead of overwriting the note with incomplete content

## [1.0.0] - 2026-03-21

### Added

- Initial release: remote MCP server for Obsidian vault via CouchDB/LiveSync
- 20 tools: notes CRUD, properties, tags, canvas, link graph, templates
- OAuth 2.1 authentication for Claude.ai and Claude Code

### Docs

- Clarified that the built-in OAuth client is pre-configured for Claude only; added instructions for registering additional clients
