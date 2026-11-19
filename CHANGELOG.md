# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Added shared bearer API key authentication via `MCP_API_KEY` for MCP clients that use `Authorization: Bearer <token>`
- Added the S3 journal backend with append-only publication, local SQLite/chunk state, URI or individual-variable configuration, and legacy/NFC path identity support
- Added `LIVESYNC_BACKEND` configuration to select CouchDB or S3 journal storage at startup
- Added strict startup configuration validation (`config.load_config`): missing variables, incomplete auth, and placeholder secrets from `.env.example` abort startup with a clear message
- Added a `backend` field to `vault_stats` output identifying the active storage backend

### Changed

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
