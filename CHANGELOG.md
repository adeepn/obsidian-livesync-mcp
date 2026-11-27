# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Journal revision trees

- Replay journal files on the S3 backend the way a device receives them, with
  the published PouchDB code ported in `revision_tree.py`: a file's records are
  filtered, sorted and merged into each document's revision tree, which is cut
  to the device's limit of 100 revisions per path. The peer now holds the
  revision a device's database holds, so a note whose conflict was resolved in
  Obsidian no longer vanishes, and a discarded version is no longer served as
  the current one.
- Refuse a file a device refuses (the chunk-record contract of its decoder,
  invalid revision, unknown reserved key, reserved identifier, a document
  named like a member of `Object.prototype`, a duplicated chunk, malformed
  attachments) instead of storing records a device drops, and keep what a
  device committed before refusing: the chunk batch, and the other records
  of a batch with one refused member. Three inputs a device accepts are
  refused as well because what it then holds depends on its engine, adapter
  or build: a revision position or hash that converts to a number that is
  not finite (any other number of a body is held, as `Infinity`), a chain
  without an element, and a supplied `_rev_tree` or attachment that is not
  well-formed.
- Receive compact chunk records as the chunk documents a device's decoder
  makes of them: a chunk the state holds is kept as it is, two records for
  one chunk in a file refuse the file after the first is written. A chunk is
  held as a document and no file is written for it; a file a release before
  chunk documents wrote is read only for a chunk no document names, so it no
  longer stands in for a chunk document.
- Check and clean attachments as PouchDB does before a batch is written, a
  revision-deleted record's included; keep only the six attachment keys it
  knows, in its order, which the deterministic revision hash is taken over;
  let a metadata stub name an attachment of the chunk batch committed just
  before, and a migrating stub one an earlier row stored. Keep every body a
  legacy state held: one that a later row's replay displaces is kept as it was.
- Convert numbers as a device does when it receives: trim exactly the
  characters `ToNumber` trims, keep the sign of a negative numeric string, turn
  a radix literal beyond every double into `Infinity`, and hold an integer
  literal beyond the interpreter's digit limit or double precision as the
  double `JSON.parse` holds. No record now fails with a Python error where a
  device has a defined value. A number that is not finite is written as
  `null` in the spelling a chunk's revision is hashed over, as `JSON.stringify`
  writes it. A revision whose hash carries a hyphen, or whose position is
  negative, is not named by its tree, as PouchDB's own read decides; a winner
  under whose name no body is stored is missing, as a device's read reports it
  before it looks at the tree.
- Walk each record over a stack of its own when it is converted, hashed and
  copied for a new revision, so a record nested as deep as the decoder admits
  is held as a device holds it instead of failing on the interpreter's call
  stack.
- Hold a document, revision or attachment-digest name that carries a lone
  UTF-16 unit, as a device holds it; the state file stores such a key with its
  units spelled out.
- Give every new revision the base a device's `put` takes its revision from,
  as a device does when a file appears over an existing entry: the plugin
  reads it with a raw `get`, which does not read the tree. Over a
  revision-deleted winner PouchDB attaches the write below the tombstone.
  Where the tree does not name the base's revision (`2-7` for the hash `7`),
  the new revision is a root above the hash it spells, as it is on a device;
  the leaves there stay as conflicts and the note is readable again. The tree
  that names the revision is the one of the document written: the chain of
  the base is not taken along. A deletion is put over the object the same
  `get` returned. A revision of a
  format PouchDB refuses, a live winner whose slot holds no body and a
  revision-deleted winner the tree does not name are refused to writers as
  they are to readers: PouchDB answers such writes with an error and writes
  nothing. A revision tombstone is still an absent note for every reader, and
  updating or deleting it changes nothing; a revision-deleted chunk counts as
  missing. A deletion of an object that is not a live note publishes nothing,
  where a device writes a revision over it.
- Publish each new revision with the chain a device reports for it, read both
  deletion flags with JavaScript truthiness on both backends, and refuse a
  winning revision the tree does not name by string to every reader, as a
  device's `get` with `revs` fails on it.
- Store each body in the by-sequence slot the IndexedDB adapter stores it in,
  `<id>::<rev>`, and read the document and the revision back off that name as
  the adapter's `decodeDoc` does. A revision that holds a colon is cut there,
  and two documents whose id and revision spell one slot share it, as on a
  device; readers and writers then see what a device's database hands them.
  A merge that stems a revision away removes its slot with the attachment
  mappings it held, as the adapter's compaction does, so over a shared slot
  the other document's body goes with it. Each attachment of a stored body is
  mapped to its slot, mappings accumulate on a slot, and the attachment data
  a device holds is held: an attachment no slot maps any more is gone, and a
  stub naming it is refused. The records of a batch are stored in the order
  the adapter's transaction stores them: every document's records start
  together and a document's next record waits for the write before it, so a
  shared slot holds the body stored last in that order. A raw `get` returns the body of the winner's slot as
  it was stored, its deletion flag included. State of earlier releases is
  replayed on first open, and a body a replay stems away is kept beside.
- Rebuild state written by earlier releases on first open, in one transaction
  taken before the schema is inspected: no body the old state held is
  dropped, not one a replay stems away or stores in another form (attachment
  data made a stub of), and a name the state file stores escaped is tracked
  as it is stored. State holding any record a device refuses, or written in a
  layout no release wrote, is moved aside and rebuilt.

### Settings parity

- Clarify that agreeing newest devices win despite older disagreement; distinguish
  nonfatal ignore-name shapes from typed policy ambiguity and its overrides.

- Compare reconnect files with device-ingested text, type and Blob size; report
  typed transformations and isolate file failures while retaining raw digests.

- Resolve recommendation-only size and ignore-file policies from advertising
  devices before stale PREFERRED; arbitrate disagreements by positive numeric
  tweakModified, then PREFERRED or a typed ambiguity with operator recovery.
  Refresh journal policy, text budget and typed provenance atomically.

- Keep valid surrogate pairs together in V1 non-plain and plain zero-cap text
  writes by extending the cut one UTF-16 unit and resetting the next nominal
  size. Recompute moved-piece IDs; preserve safe partitions, binary splits,
  reads and caller lone-unit ingestion/wire handling.

- Classify CouchDB response failures by endpoint purpose: read-only POST remains
  a typed refusal; write acknowledgements retain uncertainty and recovery.
  Refuse undeclared operations before sending with upgrade guidance.

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
