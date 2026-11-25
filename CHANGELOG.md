# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Settings parity

- Complete aggregate scans and the CouchDB metadata index by default, with
  unlimited note/byte defaults, resumable indexing, configurable time budgets,
  opaque continuation cursors and response page limits. Add fetch line windows,
  persistent isolated regex evaluation and typed per-note timeout reporting.
  Raise the negative text-verdict cache default to 65536 documents.

- Use one operator-overridable whole-note UTF-8 budget for direct text tools
  and scans across both storage types; default to the larger of the vault size
  policy and 256 MiB. Explain budget refusals with typed fields and remedies,
  allow complete beyond-budget byte downloads, and expose effective limits.
  Configure regex replacement output bytes and regex deadlines (default 10 s).

- Add CouchDB JWT authentication with plugin-exact HS256/HS512 tokens and
  ES256/ES512 raw signatures, per-backend refresh caching, validated exclusive
  auth modes and typed auth observability. Document device-field/server-key
  mapping; pin PyJWT and cryptography directly.

- Support custom headers for CouchDB and S3 auth proxies using plugin text or
  JSON configuration, with startup validation and credential-safe diagnostics.
  Apply S3 headers after signing on every request; expose typed presence/count.
  Add S3_FORCE_PATH_STYLE=false for virtual-host providers; retain true by default.

- Match terminating legacy splitter edge parameters: zero-cap V1 plain text,
  negative and JavaScript-coerced minima, empty inputs and V3 clamps. Refuse
  only proven plugin loops with setting names and recovery guidance; retain
  collision protection for surrogate pieces sharing a chunk ID.

- Read compressed CouchDB documents regardless of the vault setting and compress
  writes when `enableCompression` is true. Accept mixed device settings; report
  the effective boolean in `vault_stats`. Preserve CR/LF base64-looking text,
  surrogate pieces and leading BOMs through non-lossy write exceptions; refuse
  corrupt marked bodies with sanitized recovery guidance. Keep journal unchanged.

- Handle unreadable ignore files as absent, matching devices, instead of raising
  a NameError from the selection exception handler.

- Derive ignore-file selection independently from vault settings unless the
  operator overrides each variable. Recheck journal milestone changes and
  engine capability before writes; report typed setting sources. Vault-enabled
  ignore files require Node.js 22+. Reads bypass all selection filters.

- Guard oracle imports against stale input recipes. Cover splitter equality
  boundaries with upstream Blob/encoder/hash vectors, stored-child witnesses
  on both backends and an AST-complete comparison mutation inventory.

- Publish legacy pieces with unpaired UTF-16 units using device wire encodings:
  lossless surrogate escapes in CouchDB/metadata JSON, U+FFFD in compact journal
  leaves. Preserve piece IDs; report journal `transport_replacements` and explain
  the upstream loss in tool results. Reassemble CouchDB split pairs losslessly.

- Match the audited device's ordinary-file identities and writes across all
  supported vault settings: legacy/NFC × insensitive/sensitive paths, V1/V2/
  V2-segmenter/V3 splitters, legacy migration and five hash modes. Preserve
  UTF-16 lengths, Unicode cut boundaries, duplicate child order and empty tails.
  E2EE and path obfuscation remain unsupported (#3/#4); host reflection and
  internal/plugin sync remain outside the ordinary-file peer scope.
- Follow exact case-sensitive disk-ingestion suffixes. Support string/byte
  writes, binary attachment upload/download and editable strictly valid UTF-8
  textual newnotes (.base/.json/.yaml included). Preserve path-derived storage
  type; reject invalid plain-path UTF-8 unless upload opts into lossy conversion.
- Apply vault `syncMaxSizeInMB` (default 50 MiB, equality admitted, <=0 disables)
  instead of fixed 32 MiB write/download caps. Return structured skipped writes
  without publication; keep received oversized revisions readable. Downloads
  retain optional caller `max_bytes` without truncation.
- Add local allow/ignore regex and ignore-file selection, using pinned upstream
  ECMAScript/minimatch semantics and a persistent worker/cache. Active filters
  require Node.js 22+; with filters off the server works without Node.
- Check every advertised device/PREFERRED tweak snapshot at startup. Align
  fully advertised compatible-lossy chunk settings by `tweakModified` under
  explicit local policy; never publish remote alignment or device registration.
- Separate replay operational budgets from file admission; expose byte/count/
  depth/time overrides and actionable named errors. Preserve completed replay
  progress; report Python JSON decoder capacity independently of depth budget.
- Define applied, refused-without-change, skipped and outcome-unknown results.
  Uncertainty begins at metadata publication; lost chunk responses can leave
  harmless orphan chunks without changing the note. Re-read before retrying;
  non-idempotent edits require applying only the remaining change. Unknown move
  source removal names both paths after destination publication.

### Data safety and diagnostics

- Detect remote wipes using the newest confirmed outbound publication, reconcile
  pending outbound intents/read-back and repair last-sent tracking idempotently.
  Reset replication tracking before choosing the replay window. Preserve local
  revisions/chunks/survivors on dedupe resets; epoch changes alone preserve caches.
- Refuse oversized/excluded rename destinations before mutation to protect the
  only copy. Keep source and destination intact on a proven preflight refusal.
- Accept metadata-before-chunks journal ordering without dropping note content;
  preserve revision history and referenced historical/inline Eden chunks.
- Match legacy Blob.text BOM stripping while retaining original Blob metadata
  size; V3 retains BOM. Device reflection may decline legacy size-mismatched
  files unless `processSizeMismatchedFiles` permits them.
- Quarantine complete local state on identity changes, with restoration guidance.
  Expose local state size/warnings through `vault_stats.local_state`; never prune
  history automatically. Keep durable publication successful after bounded
  bookkeeping retries; replay repairs remaining tracking.
- Validate typed diagnostic fields at construction/rendering. Escape path
  controls while preserving identities, omit settings/remote bodies/headers and
  fingerprint epochs. Retain outcome/retry guidance and HTTP 400/405/408/413/429
  handling; mutating 408 is outcome unknown. Default storage/selection logs omit
  payloads/tracebacks; the opt-in bounded DEBUG sink withholds free text.
- Bound text scans and negative verdict caches. Keep malformed/unreadable notes
  isolated from vault-wide scans; report truncation and best-effort consistency.

### Upgrade from main

- Back up/persist each S3 peer's dedicated `S3_STATE_DIR`; do not delete it after
  a remote wipe until local-only survivors are accounted for. An identity change
  opens fresh state and archives the old directory under `.quarantine/.../state`.
- Replace assumptions about fixed 32 MiB caps with the vault policy. Restart
  after changing vault settings or env overrides; align incompatible device
  tweaks before startup. Copy/delete remain distinct from protected rename.
- Check `.env.example` and the README environment table. Durations
  `S3_REFRESH_MIN_INTERVAL_SEC` and `LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS` accept
  positive finite fractions below `2**63` (including `0.25`); byte/count/depth
  budgets require positive integers below that bound. No env variable was renamed
  in this settings-parity change. Local selection/tweak-policy overrides are
  explicit, not inferred from milestone recommendations.
- Install Node 22+ in the service PATH only when enabling selection filters.
  Encrypted plugin setup exports are not accepted as `LIVESYNC_S3_URI`; copy
  storage fields or provide a compatible `sls://` connection URI. Configured S3
  endpoints use path-style; CouchDB JWT auth/compression adapters are absent.
- Audit baseline is plugin 1.0.52/commonlib 484c164; the last real-client
  acceptance remains 1.0.50 (2026-11-20). Run the new parity live checklist
  before claiming live validation of the expanded settings/large-file scope.

### Earlier unreleased infrastructure

- Add storage-agnostic CouchDB/S3 backends, strict startup configuration,
  `LIVESYNC_BACKEND`, bearer auth and backend diagnostics. CouchDB URL defaults
  to `http://localhost:5984`; pin dependencies and provide `requirements.lock`.
- Replace the legacy CouchDB client; enforce create/overwrite, optimistic
  locking, soft-delete visibility and safe chunk publication. Fix missing DB
  startup errors, malformed canvas refusal and unknown-field preservation.
- Use round-trip ruamel.yaml frontmatter and Obsidian-compatible links/graph
  resolution; preserve comments, key order and unreadable resolution targets.
- Enforce the OAuth password form with PKCE and preconfigured Claude callbacks;
  keep DCR disabled. Document verified Codex bearer/streamable HTTP setup,
  backend connection, state monitoring and recovery.

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

- Preflight lossless CouchDB chunk collisions and admit journal collisions whose projected bytes agree, retaining IDs and replacement reporting.

- Apply device JavaScript coercion to customChunkSize, admitting terminating JSON-typed settings and naming recovery for binary data-loss or loop refusals.
