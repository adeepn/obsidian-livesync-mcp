# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Restrict `outcome unknown` to uncertain metadata publication (P-16): a lost
  chunk PUT response leaves the note unchanged; an uncertain move source
  retirement says “destination published; source removal unknown — re-read both
  paths”. S3 journals publish metadata and chunks together.
- Classify append, prepend, search-replace, after-heading insertion and canvas
  node/edge add as non-idempotent: re-read the note and apply the remaining
  change manually rather than retrying blindly. Classify overwrite, delete,
  move, copy, template/note create, attachment upload, properties set/delete,
  tags add/remove and canvas node/edge remove as idempotent for the same input:
  re-read to see whether it applied; retrying the same request is safe.
- Persist outbound journal intent before publication and confirm exact read-back;
  replay repairs stale last-sent tracking, applies/processes our journals and
  cleans unpublished intents idempotently. Missing obsolete keys no longer
  cause spurious refusal; missing newest confirmed journals still detect wipes.

- Add local device allow/ignore regex and vault ignore-file selection to both writers, with pinned ECMAScript/minimatch matching and filter diagnostics. Active filters require Node.js 22+.

- Added `obsidian_download_attachment` for complete binary downloads over base64 with raw size/SHA-256, an optional caller size limit, and no fixed peer size cap for CouchDB and S3 journals

- Added `obsidian_upload_attachment` for binary PDF/scan uploads under the vault device size policy through CouchDB or S3 journals, using LiveSync `newnote` base64 storage and returning size/SHA-256 metadata

- Added S3 local peer state size/path diagnostics and `vault_stats.local_state` warnings controlled by `S3_STATE_SIZE_WARN_MB` (disabled by default)
- Added shared bearer API key authentication via `MCP_API_KEY` for MCP clients that use `Authorization: Bearer <token>`
- Added the S3 journal backend with append-only publication, local SQLite/chunk state, URI or individual-variable configuration, and legacy/NFC path identity support
- Added `LIVESYNC_BACKEND` configuration to select CouchDB or S3 journal storage at startup
- Added strict startup configuration validation (`config.load_config`): missing variables, incomplete auth, and placeholder secrets from `.env.example` abort startup with a clear message
- Added a `backend` field to `vault_stats` output identifying the active storage backend

### Changed

- Keep completed journal writes successful when processed/last-sent bookkeeping
  fails after commit; retry tracking with a bound, warn on exhaustion, and let
  idempotent replay repair tracking.
- Scope mutation preservation to peer-initiated refusals; incoming replay keeps
  remote changes already applied before a later failure.

- Replace fixed 32 MiB upload/write caps with the resolved device `syncMaxSizeInMB` rule on both backends; oversized ordinary writes and copies return a structured skipped outcome without publishing or changing the previous revision. Keep delete and received revision persistence independent of this gate; refuse oversized rename destinations before any mutation because this peer lacks the device's retained host file.
- Remove the fixed download cap; retain the optional caller `max_bytes` limit.
- Expose operational journal/replay budget thresholds through environment overrides (JSON decoding remains constrained by Python recursion capacity), enlarge defaults with piece-derived headroom, and return actionable budget errors while preserving completed replay progress.

- Resolve plain/newnote storage from the exact case-sensitive LiveSync device disk-reading suffixes in both backends; accept strings and bytes through one writer and retain attachment aliases; size policy now uses the vault limit rather than fixed peer caps
- Expose strictly valid UTF-8 newnotes as editable text in MCP tools while retaining path-derived storage types; keep metadata/download behavior for non-UTF-8 attachments

- Expanded the connection guide with plugin credential sources, both S3 configuration forms, a complete environment reference, local replica monitoring/reset procedures, and the native-peer architecture rationale
- Rewrote the storage layer: all 20 tools now operate through a storage-agnostic `StorageBackend` interface, with a LiveSync-aware `CouchDBBackend` implementation (soft-delete visibility, chunked documents, optimistic locking, error messages free of internal URLs and revisions)
- Rewrote frontmatter handling on `ruamel.yaml` round-trip editing: comments, quoting and key order are preserved; tag add/remove coerce types instead of corrupting YAML
- Rebuilt the link graph with an Obsidian-compatible resolver (heading anchors, aliases, markdown links); unreadable notes stay in resolution instead of being dropped
- `COUCHDB_URL` now defaults to `http://localhost:5984`, matching README and `.env.example`; all remaining defaults are documented in the README environment table (audit A4)
- Pinned dependencies (`mcp[cli]==1.30.0`) and added a frozen `requirements.lock` (audit A1)

### Fixed

- Write tools enforce data-safety semantics: no-op writes are skipped, create/overwrite contracts are explicit, and failed writes can no longer point a note at missing chunks
- Canvas tools refuse empty or malformed structures without mutating the file, and preserve unknown keys of spec-valid documents
- OAuth mode gates `/authorize` behind the `OAUTH_PASSWORD` form before issuing any code; dynamic client registration stays disabled
- A missing CouchDB database is reported as a storage error at startup instead of surfacing as "note not found"

### Removed

- Removed the legacy `couchdb.py` client; `storage_couchdb.CouchDBBackend` supersedes it

### Docs

- Documented verified Codex streamable HTTP MCP configuration using `bearer_token_env_var = "MCP_API_KEY"`
- Added explicit custom header key/value guidance for MCP clients that configure headers manually
- README: added a "Backends" section (CouchDB and S3 journal storage), a defaults column in the environment table, and honest install/run steps

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
