# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

- Bound CouchDB inflation before data projection and publication; expose LIVESYNC_COUCHDB_MAX_INFLATED_BYTES for larger legitimate pieces.

- Withhold unstructured SDK transport and validation logs at the hosting boundary.

- Configure HTTP request bodies with LIVESYNC_MAX_REQUEST_BYTES (1 GiB default, 0 unlimited); retain the separate OAuth form cap.

- Repair historical scan-policy links and name the selection deadline override.

- Keep setup recipes ahead of alternatives and move detailed configuration, differences, JWT provisioning and developer acceptance to focused references.

- Correct confirmed remote-wipe recovery: create service records once, then explicitly rebuild the disposable server from device files.

- Prevent reporting, startup, hosting and shutdown exceptions from exposing remote/configuration text through log tracebacks.

- Keep Node capability probes out of short integration deadlines and widen subprocess/rendezvous scheduler margins in tests.

- Bound expanded frontmatter string and key bytes before property serialization; expose LIVESYNC_FRONTMATTER_MAX_BYTES with a 4 MiB default and raw-note recovery.

- Lower the default frontmatter projection budget to 20,000 expanded nodes.

- Confirm ambiguous conditional journal retries at the original key before reporting success; retain recovery state when read-back cannot prove publication.

### Settings parity

- Resolve YAML anchors and aliases in properties/tags, preserve untouched key bytes during edits, and expose configurable expanded-node/depth budgets with typed recovery guidance.

- Retain outcome uncertainty when a conditional S3 retry ends in a pre-dispatch failure or unsupported response after an earlier response may have been lost.

- Make CouchDB, S3 and selection deadlines configurable; allow configured read/conditional S3 retries while retaining single-attempt unconditional publication.

- Consolidate connection instructions, settings differences, refusal remedies and oracle regeneration provenance.
- Require explicit write/delete authorization and exact target confirmation in the live acceptance helper; withhold unknown remote setting names and classify reconnect files using resolved vault policy.
- Bind documented environment defaults and validation rules to actual configuration consumers; restore current-source splitter mutation witnesses.

- Omit supplied values from startup configuration errors while retaining variable names and validation rules.

- Complete real-server acceptance for per-row text/binary classes, large files, recovery, wire headers, compression, surrogate refusals and all JWT algorithms; retain native large-file oracle provenance.

- Explain the plugin PKCS8 requirement and provide actionable SEC1 conversion guidance for EC JWT keys.

- Verify JWT session identity at startup and classify invalid-token HTTP 400/401 as typed authorization failures without exposing remote reasons.

- Refuse complete CouchDB writes containing unpaired UTF-16 units before publication, matching real CouchDB rejection of device-shaped JSON.

- Add env-gated real-server pairwise and surrogate acceptance tests and JWT probes; retain failed acceptance findings without changing production behavior. Update stale epoch and chunk audit expectations.

- Guard regenerated JWT timestamps and exact HMAC token bytes across two successive cache boundaries.

- Convert nested scalar arrays iteratively for size and splitter parameters, preserving native join cycle and empty-field semantics.

- Revalidate a restored active policy after candidate failure so force-refresh clears stale Node capability diagnostics before another write.

- Validate candidate selection capability before exposing journal policy changes, including missing-Node refusal and awaiting checks.

- Mirror JavaScript numeric coercion for vault size settings and truthiness for ignore enablement across all JSON types; compare effective device policies and retain strict operator overrides.

- Explain recovery for conflicting or encrypted shared journal leaves while retaining both pre-publication protection checks.

- Guard exact-positive-integer replacement counts against booleans and other JSON types in the compatibility notice wrapper.

- Guard verbatim publication-result preservation for whitespace, blank lines, multiline JSON and non-JSON text.

- Cover repeated V2 array-minimum concatenation with native splitter pieces and IDs through the fourth adjustment.

- Guard inactive ignore-file policies against validation of conflicting or wrongly typed names on either backend.

- Commit journal selection, size and derived text policy atomically after candidate validation; preserve prior settings on refusal.

- Pin zero/five-minute JWT cache boundaries across all algorithms and document empty expiry refusal and the upstream string-zero distinction.

- Disclose reserved CouchDB prefix interpretation on text writes, preserve valid marked-piece reuse and refuse undecodable new pieces before publication with binary-path recovery.

- Preserve parameter coercion evaluation order on empty/NaN inputs, numeric array overflow and terminating zero-string additions; expose actionable typed writer refusals through MCP.

- Make timeout-tool tests deterministic, coordinate concurrent test operations with events and widen tight scheduler-sensitive smoke-test margins.

- Accept zero CouchDB JWT expiry with the device five-minute fallback; explain negative-duration refusal as already-expired upstream tokens.

- Re-resolve journal size admission and derived text budgets at runtime milestone refresh while preserving explicit overrides.

- Accept device ignore-file name lists without startup refusals; retain JS trim/empty entries, bound lookup to vault notes and warn on unresolved explicit names.

- Preserve every original publication result field, including object-valued messages, when appending transport notices.

- Preflight lossless CouchDB chunk collisions and admit journal collisions whose projected bytes agree, retaining IDs and replacement reporting.
- Apply device JavaScript coercion to customChunkSize, admitting terminating JSON-typed settings and naming recovery for binary data-loss or loop refusals.
- Preserve JavaScript ToPrimitive and addition in V2 minimum adjustment; remove the integer shortcut and its invalid array-equivalence proof.
- Disclose BOM stripping, invalid UTF-8 and surrogate replacements through typed write notices and fixed prose outside the original tool result; retain device partitions, IDs and metadata sizes.

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
  proven plugin loops and terminating V2 binary data loss with setting names
  and recovery guidance; retain collision protection for shared chunk IDs.

- Read compressed CouchDB documents regardless of the vault setting and compress
  writes when `enableCompression` is true. Accept mixed device settings; report
  the effective boolean in `vault_stats`. Preserve CR/LF base64-looking text
  and leading BOMs with uncompressed writes; refuse surrogate-containing
  publications that CouchDB cannot store and corrupt marked bodies with
  sanitized recovery guidance. Keep journal unchanged.

- Handle unreadable ignore files as absent, matching devices, instead of raising
  a NameError from the selection exception handler.

- Derive ignore-file selection independently from vault settings unless the
  operator overrides each variable. Recheck journal milestone changes and
  engine capability before writes; report typed setting sources. Vault-enabled
  ignore files require Node.js 22+. Reads bypass all selection filters.

- Guard oracle imports against stale input recipes. Cover splitter equality
  boundaries with upstream Blob/encoder/hash vectors, stored-child witnesses
  on both backends and an AST-complete comparison mutation inventory.

- Use device JSON/TextEncoder wire encodings, preserving original piece IDs
  and reporting journal `transport_replacements` for compact-leaf U+FFFD loss.
  Refuse CouchDB publications with lone-surrogate units before sending any leaf,
  because the server rejects those JSON escapes, including device-shaped writes.

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
  in this settings-parity change. Explicit local overrides take precedence;
  ignore enablement/names otherwise derive independently from vault settings.
  Regex lists and tweak-mismatch/auto-accept controls remain local configuration.
- Install Node 22+ in the service PATH only when enabling selection filters.
  Encrypted plugin setup exports are not accepted as `LIVESYNC_S3_URI`; copy
  storage fields or provide a compatible `sls://` connection URI. Configured S3
  endpoints support path or virtual-host addressing through S3_FORCE_PATH_STYLE.
  CouchDB supports explicit Basic/JWT auth and vault-controlled compression.
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
