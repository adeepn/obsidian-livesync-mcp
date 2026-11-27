# Peer limits and refusal inventory

This inventory records the 2026-11-25 Task D source audit. `C:` means
commonlib `484c164450d61deb2e301582b60cb9b5f9fa977f/src/`; `P:` means plugin
`5070b67beae3b60e3a79132386a08da3f1ef213a/src/`. References describe source
behaviour, not acceptance of every possible input by an Obsidian client.
Defaults below are peer resource choices unless explicitly called upstream
semantics. They must not be interpreted as LiveSync protocol maxima.
See the [environment reference](configuration.md#environment-reference) for overrides.

## Deletion and conflict policies

| Policy | Pinned plugin behaviour | Peer behaviour and action |
| --- | --- | --- |
| `deleteMetadataOfDeletedFiles` | Ordinary deletion sets `deleted=true` and mtime; when enabled it also sets `_deleted=true` (`C:managers/EntryManager/EntryManagerImpls.ts:765-775`). Incoming reflection treats either marker as deletion (`C:serviceModules/ServiceFileHandlerBase/reflection.ts:240-257`). | Both backends always publish soft deletion, retaining body references, never purge history (`storage_couchdb.py:1252-1272`, `storage_journal.py:1318-1334`). Devices honour that marker; this does not claim filesystem reflection succeeds against every host failure. Agree retention separately; perform intentional history maintenance with LiveSync. |
| `automaticallyDeleteMetadataOfDeletedFiles` | Positive days expires `deleted` metadata by setting `_deleted`; zero disables (`C:serviceFeatures/offlineScanner/metadata.ts:437-482`). | No timed cleanup; deletion history remains until separately maintained. Default 0 agrees on no expiry; positive settings differ. |
| `disableMarkdownAutoMerge`, `resolveConflictsByNewerFile` | May merge content, write it, delete the losing revision, or select by mtime (`P:serviceFeatures/conflictResolution/operations.ts:124-175`). | No conflict automation. CouchDB refuses mutations of unresolved sibling notes (`storage_couchdb.py:962-967,1076-1080,1267-1272`). Journal keeps every sibling and holds the revision a device's database holds: each file is replayed through the device's receive path and PouchDB's winner rule (`revision_tree.py`, compared with the published database after every file in `tests/test_journal_winner.py`); that is not newest mtime. Resolve in Obsidian before retrying a refused edit. |
| `writeDocumentsIfConflicted` | Permits filesystem reflection despite conflicts; false queues resolution (`C:serviceModules/ServiceFileHandlerBase/reflection.ts:223-233`). | No filesystem reflection; does not override the CouchDB write refusal or change the journal winner. |
| `processSizeMismatchedFiles` | False prevents filesystem reflection when nonzero recorded size differs from Blob size (`C:serviceModules/ServiceFileHandlerBase/reflection.ts:273-282`). | Direct database access; no device reflection stage. Own lossy transformations emit notices; inspect them before expecting every device to show the file. |
| `trashInsteadDelete`, `doNotDeleteFolder` | Chooses host trash/delete and whether empty parent folders are removed (`C:serviceModules/ServiceFileAccessBase.ts:383-405`). | No host files/folders/trash. Database soft-delete semantics above apply independently. |
| `doNotUseFixedRevisionForChunks` | Retained in defaults (`C:common/models/setting.const.defaults.ts:177`) and configuration metadata; `C:API/DirectFileManipulatorV2.ts:344-345` propagates it and `P:apps/webpeer/src/P2PCheckSetup.ts:158` lists it, but no active chunk-manager/database consumer reads it at the pin. | Not a separate peer control. This conclusion comes from references in both pinned trees, excluding tests and metadata declarations. |

## Operational and tool bounds

“Default impact” asks whether the peer can stop a case that a device serves with
its default vault settings. “Yes” is not a claim that a device has infinite
memory or that every server permits that input.

| Bound / refusal family | Source and upstream comparison | Default impact, reason and caller action |
| --- | --- | --- |
| File size: default 50 MiB; strict `>`; equality allowed; nonpositive disables | `storage_base.py:check_file_size`; `C:services/base/VaultService.ts:73-81`, defaults `C:common/models/setting.const.defaults.ts:147`. Numeric scalar coercion and two multiplications mirror JavaScript. | No additional file-size gate. Override locally only when intentional; device settings still determine reflection. Downloads bypass this gate. |
| Journal lines 256 MiB; inflated/object bytes 8 GiB; records/refresh keys 1,000,000; depth 512; refresh 64 GiB/3600 s; positive values below `2**63` | `replay_limits.py:ReplayLimits`; `journal_codec.py`, `object_storage.py`, `storage_journal.py` consume them. Upstream downloads, inflates and decodes without these peer budgets (`C:replication/journal/JournalSyncCore.ts:895-920`). | Yes, history is unbounded even with default 50 MiB files. Protects memory/disk/CPU against hostile or unexpectedly large journals. Error names exact override; raise it and restart, then continue refresh. Decoder recursion is a separate runtime ceiling; raising depth alone cannot remove it. [Derivation/recovery](size-policy.md). |
| CouchDB HTTP wire and decoded response bytes: resolved splitter budget, 32 MiB floor | `storage_couchdb.py:_request`; upstream delegates to fetch (`C:services/base/RemoteService.ts:340-380`) without this peer cap. Every request forces `Accept-Encoding: identity`; gzip/deflate responses are incrementally decoded under both caps. | Protects against HTTP-level decoding inflation before JSON or document inflation. Single/control/attachment and `_bulk_docs` acknowledgement responses each use one budget; requested `_all_docs` document batches multiply it by the local key count. The full JSON envelope must fit. Raise `LIVESYNC_COUCHDB_MAX_INFLATED_BYTES` and restart for legitimate larger responses; re-read after an unconfirmed write. Unknown, stacked or malformed encodings refuse: configure the server/proxy to return identity. |
| Publication refresh reserve: 64 KiB before sync parameters are measured, then their bytes + 1024 | `storage_journal.py:1115-1122`; upstream consumes sync parameters before journal processing (`C:replication/journal/JournalSyncCore.ts:168-175,895-920`). | Normally below 64 GiB refresh budget; can refuse a tightly configured budget. Raise refresh bytes to include control reads as the error instructs. It is resource headroom, not a device maximum. |
| Whole-note text budget: max(positive vault size, 256 MiB); 0 unlimited | `text_eligibility.py:TextAccessPolicy`, both backends/server; upstream file size predicate has no separate text projection cap (`C:services/base/VaultService.ts:73-81`). | Can affect stored text received from larger-policy peers, even under a current default vault policy. Resource protection; set `LIVESYNC_TEXT_SCAN_MAX_BYTES=0` or raise it. Fetch explains the reason; byte download remains available; edits leave the old revision. |
| Strict UTF-8 and printable/control validation for textual newnotes | `text_eligibility.py:TextProjection,project_note`; newnotes are binary Blob data upstream (`C:managers/EntryManager/EntryManagerImpls.ts:253-280,655`, `C:string_and_binary/chunks.ts:13-72`). | Binary remains binary; NUL/C0 (except tab/CR/LF) and C1 control bytes do not become tool text. Use attachment download/upload for arbitrary bytes. This is a tool representation choice, not device file rejection. |
| Aggregate scans: no default note/byte caps; 20 s including waiting; two concurrent slots; responses page by 200; search default 200 matches | `scan_budget.py`, `server.py:_vault_scan`; upstream returns files without these MCP result budgets (`C:serviceModules/ServiceFileAccessBase.ts:287-290`). | Yes on large/slow vaults or contention. Follow `next_cursor`; inspect `truncated`, `stopped_because` and index completeness. Set scan seconds to 0 for no deadline. Cold CouchDB index needs further calls; narrowing a filter alone does not complete an unfinished index. [Exact semantics](scan-policy.md). |
| Regex requests 10 s; replacement output defaults to effective text budget | `regex_runner.py:apply`; `server.py` passes configured values. Plugin selection uses native ECMAScript regexes (`C:serviceFeatures/targetFilter.ts:153-209`); MCP search/replace is an additional Python-regex tool. | Yes for slow regex/tool requests, not a file sync cap. Raise timeout/output budget as named by the refusal; use a simpler expression or literal replacement. Timeout is positive, cannot be disabled with 0. |
| Selection worker configurable 5 s deadline; 1 MiB response line; one request/response slot; stderr keeps 8 × 1024-byte chunks; 1 s reap/join | `selection_engine.py:TIMEOUT,SelectionWorker`; native target filtering has no corresponding 5 s deadline (`C:serviceFeatures/targetFilter.ts:132-149,153-209`). | Filters are off by default; valid slow patterns can still fail when enabled. Protects the MCP process from stuck JavaScript. Install Node.js 22+; simplify pathological patterns or use a service with sufficient CPU. Raise `LIVESYNC_SELECTION_TIMEOUT_SECONDS` (positive finite seconds) and restart for a valid slow policy. The closed response is small; line/stderr bounds protect worker transport. |
| Text verdict LRU 65,536 (configurable); selection verdict LRU 4096 | `config.py`, `device_selection.py`, `journal_store.py`; upstream caches ignore-file reads (`C:serviceFeatures/targetFilter.ts:115-126`). | No admission loss: eviction recomputes verdicts. Raise text-cache entries only for performance. These are caches, not vault/file maxima. |
| CouchDB 5 s connect, 30 s read/write/pool; S3 5 s connect, 30 s read, one SDK attempt by default | `storage_couchdb.py:201`, `object_storage.py:127-130`. Upstream S3 config requests four attempts (`C:replication/journal/objectstore/MinioStorageAdapter.ts:70-80`); device transport differs. | Yes on slow/unreliable servers. Fixed transport policy bounds hangs and avoids hidden retries of ambiguous writes. Check network/server health; re-read after an outcome-unknown mutation before retrying. Raise `COUCHDB_CONNECT_TIMEOUT_SECONDS` / `COUCHDB_IO_TIMEOUT_SECONDS` or `S3_CONNECT_TIMEOUT_SECONDS` / `S3_READ_TIMEOUT_SECONDS` and restart. `S3_MAX_ATTEMPTS` tunes reads and conditional creates; unconditional PUT remains one attempt. See [retry matrix](s3-journal.md#transport-policy). |
| S3 list page 1000; CouchDB index page 500; text fetch window 50; read blocks 1 MiB; regex pipe blocks 64 KiB/32 KiB | `object_storage.py`, `storage_couchdb.py`, `storage_journal.py`, `regex_runner.py`; upstream also paginates S3 listing (`C:replication/journal/objectstore/MinioStorageAdapter.ts:221-234`). | No total-result admission bound by themselves; loops paginate/stream. A nonadvancing list token refuses rather than hang. Repair server pagination; do not infer missing keys from a partial listing. |
| Ten S3 candidate journal names; 128-bit random suffix; exists+put fallback | `storage_journal.py:1132-1163`; upstream timestamp journal naming/upload (`C:replication/journal/JournalSyncCore.ts:675`, `C:replication/journal/objectstore/MinioStorageAdapter.ts:132-145`). | Name-collision protection, normally negligible; exhausted allocation refuses. Refresh and inspect the store before retrying. Stores without conditional writes cannot exclude a racing writer; use conditional-write-capable storage for stronger guarantees. |
| Revision position or hash that converts to a number that is not finite, an array-like `ids` without an element, a supplied `_rev_tree` or an attachment that is not well-formed | `revision_tree.py:_finite,_identifier,_supplied_tree,_admit_attachments`. A device accepts these; what it then holds depends on its JavaScript engine, adapter or build. A `_revisions.start` that converts to a number that is not finite (missing, `"NaN"`, `"Infinity"`, `"1e400"`, a radix literal or a digit string of thousands of digits) is kept at that position while the batch runs; both adapters store a tree as JSON, where it becomes `null`. Over such a root pouchdb-merge subtracts `null` from a position, takes the root for its own parent and throws (`lib/index.js` `doMerge`, `el.parent[2]`), in a callback of the adapter: in the level adapter the exception never reaches its caller and the receive of that file does not complete (`missing-start`, `start-overflow`, `own-rev-digits-overflow-prefix`, `chunk-plus-start-radix-overflow`; `empty-ids` in the winner fixture). A malformed supplied tree fails inside its adapter; attachment data that is not a string is taken for a Blob, which JSON cannot carry. | The file is refused and stays unprocessed; the peer holds no state over which a device cannot complete a later merge. Revisions themselves are unbounded: a device holds a generation as a double and the peer keeps the same spelling, so `9223372036854775808-a` is held as `9223372036854776000-a`. |
| Local adapters disagree on one malformed revision, on a number beyond every double and on a revision a slot's key cannot spell | Devices default to the IDB adapter (`P:src/common/core/settings/compatibilitySettings.ts:13-18`). The legacy IndexedDB adapter and the level adapter share its tree code but ignore a record whose revision already has a stored body while the tree does not name it (pouchdb-adapter-leveldb-core 9.0.0 `lib/index.js:723-729`; pouchdb-adapter-indexeddb 9.0.0 `lib/index.js:703`); IDB merges it again. The level core also stores a document body as JSON (`encode: safeJsonStringify`), where such a number is `null`; IDB keeps it. | The peer follows IDB. It takes a revision `revExists` cannot find, such as `null-b`, and holds a number beyond every double as IDB does; well-formed revisions behave the same on every adapter. The unmodified memory adapter is run beside the IDB oracle and the fifty-one cases where they differ are recorded: `level-ignored-kept-revision`; the eight `rev-*` cases of a revision that holds a colon and the twenty-six `slot-collision*` cases, where the level core stores a body by sequence and never decodes a revision off a key; `attachment-mapping-accumulates` and `attachment-compaction-order`, where it drops an attachment the IDB adapter still maps; `messy-45`, `messy-50`; the seven `number-*` cases of a number beyond every double in a note's body and the five `chunk-nonfinite-hash*` cases of one in a chunk body (its revision is the same on both: the hash is taken over the spelling `JSON.stringify` gives such a number, `null`, before anything is stored). |
| A winning revision the tree does not name by string | pouchdb-core `get` splits the revision at every hyphen and looks for the second part among the hashes of a path; it finds none and fails with `invalid rev tree` for a numeric hash, where the revision `2-7` does not equal the number `7`; for a hash with a hyphen, where `1-a-b` makes it look for `a`; and for a negative position, where `-2-x` makes it look for `2` (a start of `"-2"` keeps its sign). | No reader, list or scan serves it: reading it reports `is corrupt: the revision tree does not name the winning revision`, and so does copying or moving it, which reads the note first. The plugin's writer does not read the tree: its raw `get` returns the body and its `put` with `force` makes the new revision a root above the hash the revision spells (`3-…` above `7`), where the old winner stays as a conflict; the peer publishes that revision, and the note is readable again. Where no body is stored under the winner's name (a start that is not spelled as its number, such as `" 2 "`, keeps its spelling in the stored name), a device's `get` reports the note missing, and so do the peer's readers; PouchDB answers a write over it with a conflict, and the peer refuses it with `is corrupt: the winning revision has no stored body`. A file of an earlier release does not stand in for such a chunk document either. |
| The slot a body is stored in | The IDB adapter stores a body in its by-sequence store under `<id>::<rev>` and reads `_id` and `_rev` back off that key, cut at its last colon (pouchdb-adapter-idb 9.0.0 `lib/index.js:75-83,472`). A revision that holds a colon does not survive it (`1-a:b` is read back as `_id` `n.md::1-a`, `_rev` `b`): the note cannot be read with its chain, and a write over it is refused as `Invalid rev format`. Two documents whose id and revision spell one slot share it, the body written last serving both: a raw `get` of the first returns the second's body, a file put over the first descends from the second's revision (a new root where the first's tree does not name it), and a deletion of the first is written into the second document. The plugin's writer and deleter read their base with that raw `get` (`C:managers/EntryManager/EntryManagerImpls.ts:295,746`) and `put` with `force` (pouchdb-core 9.0.0 `lib/index.js:419-432`), which names the base's hash for the only ancestor of the new revision and lets the merge find it in the tree of the document written: over a shared slot that is not the document the base came from, and the rest of the base's chain is not taken along. A raw `get` returns the body of the winner's slot as it was stored, its flags included; the plugin reads `_deleted` off the body (`EntryManagerImpls.ts:435`), so a note whose winner's slot holds another document's tombstone is absent for it. A merge that stems a revision away removes the slot it was stored in, with the attachments it mapped (`finishDoc`, `compactRevs`, pouchdb-adapter-idb 9.0.0 `lib/index.js:476-485,173-241`): over a shared slot the other document's body goes with it and that note is missing. Every attachment of a stored body is mapped to its slot (`insertAttachmentMappings`); an attachment no slot maps any more is gone, and a stub naming it is refused. The records of one batch are written in the transaction's own order: every document's records start together (`processDocs`), a document's next record only after the write before it, a request is served in the order it is made and its success event is dispatched before the next request is served, attachment data before the body it belongs to. | Bodies are stored in the same slots and read back through the same decoding (`journal_store.py:_seq_key,_decoded`); state of earlier releases is replayed on first open. Readers follow IDB's `get` with `revs: true`, writers and deletions its raw `get` and the plugin's `put`, as recorded for twenty starting states: a revision the tree does not name makes the new revision a root, the leaves there staying as conflicts, and a revision the write made may itself lose to a leaf. PouchDB answers a write over a live winner whose slot is empty, or over a revision-deleted winner the tree does not name, with a conflict, and the peer refuses it the same way. A merge stems a revision away: its slot and attachment mappings are removed, and a legacy body that slot held is kept in `legacy_bodies`. The records of a batch are stored in the transaction's own order (`revision_tree.write_batch`), so over a shared slot the body stored last is the first document's second record, not the batch's last. A deletion over an object that is not a live note (untyped, already marked `deleted`, or another document's tombstone in a shared slot) publishes nothing, where a device writes a revision (`_deleted` for the first, another `deleted` for the second, a deletion in the other document for the third); that is the deletion policy above. A deletion over a body that carries another path than its own document's (another document's body in the winner's slot, with this note's path) is refused as `metadata path identity mismatch or collision; rebuild/fetch required`: the plugin would put that object as it is, a record its own rebuild check rejects (`C:common/pathIdentity.ts:51-55`). |
| State written by earlier releases | Every record was stored under its `_rev` in arrival order, without revision trees. | On first open each record is replayed again through the receive path, in one transaction taken before the schema is inspected; a record a device does not write keeps its body as history. No body the old state held is dropped: a row whose body no stored revision holds any more, because a later row's replay took its place or stemmed its revision out of the tree, and a row whose replay stored something else than the record (attachment data made a stub of), is kept as it was in the `legacy_bodies` table before the old table goes. Any record a device refuses, thrown or returned, moves the whole state directory to `.quarantine/` and it is rebuilt from the journal; so does a layout no release wrote (a development build between the releases stored bodies by revision). An arrival order that only stemming distinguishes is rebuilt by a full replay after a wipe. |
| Numeric strings and number literals | `_revisions.start` reaches the tree through ToNumber: the 25 characters of WhiteSpace and LineTerminator are trimmed, a sign is kept, `Infinity`, decimals and radix literals are converted, and what exceeds every double is `Infinity`. A revision's position is converted with `parseInt`. `parseDoc` concatenates the start it was given, so a padded start names the body with its padding while the tree names the winner `2-x`, which a device's `get` reports as missing. JSON's integers are doubles: one a double cannot represent exactly is rounded, one beyond their range is signed `Infinity`, and the interpreter's limit of 4,300 decimal digits does not apply. | Converted exactly so (`revision_tree.py:_string_number,_parse_int,_decimal_integer`, `journal_codec.py:_json_integer`). The fixture records `Number` and `parseInt` of 2,118 strings, every trimmed character, and whole files around them; a number beyond every double is held as IDB holds it. |
| Compact chunk records | The plugin's decoder makes each `~h:<id>\x1f<data>` line a chunk document without a revision (`C:replication/journal/journalRecords.ts:31-50`), and it takes part in the file's chunk batch as a full-JSON chunk record does (`journalReceive.ts:17-29`): a chunk the device holds is kept as it is, and two records for one chunk in one file fail the batch after the first is written. | Received exactly so from the file's records in order (`journal_codec.py:JournalFile.records`, `journal_store.py:apply_journal`). A chunk received is held as a document and no file is written for it; a file a release before chunk documents wrote is read only for a chunk no document names and counts as held for admission. An identifier outside the cache's alphabet does not refuse the record; the document is held, and a note referring to it is refused as an invalid chunk reference. |
| A document, revision or attachment-digest name with a lone UTF-16 unit | A string used as `_revisions.ids` names its UTF-16 units, so `"😀"` gives the hashes `\ud83d` and `\ude00`; a hash, an id or a digest may itself be such a unit. A device holds it. | Held and served as spelled. SQLite TEXT is UTF-8, so the state file stores such a key with its units spelled out behind a marker (`journal_store.py:_db_key`); every other key is stored as it is. |
| Attachments (`_attachments`), which LiveSync never writes | core `bulkDocs` checks every record of a batch before anything is written, a revision-deleted one included: an attachment name may not start with an underscore, and each attachment is read for its content type (pouchdb-core 9.0.0 `lib/index.js:968-978`). `cleanDocs` then drops the attachments of a deleted record and keeps only `data`, `digest`, `content_type`, `length`, `revpos` and `stub` on every other attachment, in that order, which is the order the deterministic revision hash is taken over (`lib/index.js:224-239`). `preprocessAttachments` requires base64 data, decoded by the browser build with the platform `atob` (whitespace ignored, padding optional only at a multiple of four characters); `verifyAttachments` requires a stub to name an attachment the device holds, never one of its own batch. | Admitted exactly so (`revision_tree.py:_prepared,_admit_attachments`). The stored body keeps `content_type`, `length`, `digest`, `revpos` and is served with `stub: true`, as a device serves it; an array of attachments stays an array. A stub may name an attachment of the chunk batch committed just before, as on a device, and a legacy migration sees the digests it has just written. An attachment or a container that is not an object fails the batch, as it fails on a device. Each attachment of a stored body is mapped to the slot of that body (`attachment_digests`), and the attachment data a device's attachment store holds is held in `attachment_data`: a stub is admitted while it is there. The peer's own notes never carry attachments. |
| A document named like a member of `Object.prototype` (`constructor`, `toString`, `__proto__`, …) | The plugin's `journalMetadataRevisions` spreads a plain object and cannot iterate the inherited member (`C:replication/journal/journalReceive.ts:46-51`); the receive fails after its chunk batch. | The file is refused the same way (`object-key-id`, after the chunk batch) and stays unprocessed. Such a record needs a vault file with no extension and exactly that name. |
| One document with very many sibling revisions | The port keeps PouchDB's own tree algorithms (`revision_tree.py:_do_merge,_merge_tree,stem`): each merge walks the existing branches and stemming rebuilds every root-to-leaf path. Measured on a synthetic store: a batch of 100 sibling leaves of one document takes 77 ms, 500 take 5.1 s, 1,000 take 37 s; ordinary vaults stay near linear (10,000 documents: 0.22 s replay, 68 ms `note_ids`). | No limit is imposed. Only unresolved conflicts of a single note accumulate; resolving them in Obsidian removes the cost. An optimisation must preserve PouchDB's order and alias semantics and be checked against the oracle. |
| Frontmatter conversion: 20,000 expanded nodes, 4 MiB expanded UTF-8 strings/keys and depth 200; aliases resolved | `frontmatter.py` event preflight and memoised conversion; plugin syncs file bytes without parsing YAML (`C:services/base/VaultService.ts:73-81`; `C:managers/EntryManager/EntryManagerImpls.ts:253-280`). | Ordinary 10,001-item lists, depth 34 and reused aliases succeed. Expanded bombs/cycles and pathological nesting decline only properties/tags with typed reason; the note remains intact and synced. Raise `LIVESYNC_FRONTMATTER_MAX_NODES` / `LIVESYNC_FRONTMATTER_MAX_BYTES` / `LIVESYNC_FRONTMATTER_MAX_DEPTH` and restart, subject to the real parser recursion ceiling, or fetch/edit raw note text. [Memory/time and recovery](frontmatter-properties.md). |
| MCP authorization form body 4 MiB; auth-code/token lifetimes | `oauth_provider.py:AuthorizePasswordGate,SimpleOAuthProvider`: SDK-sized form cap, codes 300 s, access 86400 s, refresh 30 days. Obsidian LiveSync has no MCP OAuth endpoint. | Not a vault-file limit. Submit a normal authorization form; renew authorization when expired. Client auth, configured port `1..65535`, backend/group exclusivity and credential/key validation are connection requirements. See [auth](clients-and-authentication.md) and [connection rules](connecting-storage.md). |
| Diagnostic numbers below `2**63`, exit statuses -256..256; debug sample 2048 characters; digest prefixes 16 hex | `diagnostic_hygiene.py:numeric_field,unsafe_debug`; plugin logs are not the peer public diagnostic contract. | Output hygiene only; invalid numeric fields become fixed markers, not admission refusals. Never enable unsafe debug to recover credentials; use typed reason/count fields. |

## Data-protection refusals and upstream semantics

| Refusal family | Pinned behaviour and peer reason | Remedy |
| --- | --- | --- |
| E2EE, obfuscated paths, encrypted chunks | Plugin supports encryption; peer does not implement it (`livesync_format.py:validate_vault_settings`; upstream encryption/identity sources inventoried in [the specification](superpowers/specs/2026-11-23-settings-parity.md)). | Use an unencrypted/unobfuscated test vault until issues #3/#4 are implemented. Do not disable encryption on a working vault just to connect this peer. |
| Missing/unreadable/disagreeing effective identity, splitter or required tweak snapshots | Peer cannot choose a safe writer policy without an authoritative or agreeing map (`livesync_format.py:resolve_*`; upstream tweak checks `C:common/models/tweak.definition.ts:1-138`, identity `C:services/base/PathService.ts:86-105`). | Align devices/PREFERRED or use the named documented override; refresh/restart. |
| Unresolved recommendation-only ambiguity (`syncMaxSizeInMB`, `useIgnoreFiles`, `ignoreFiles`) | Devices that agree give their common value without any timestamp, and a key that nobody advertises takes its device default. The peer refuses only when the devices disagree and neither the timestamp arbitration nor PREFERRED decides (`livesync_format.py:resolve_recommendation_policy`). | Align devices/PREFERRED or set `LIVESYNC_SYNC_MAX_SIZE_MB`, `LIVESYNC_USE_IGNORE_FILES` or `LIVESYNC_IGNORE_FILES`; refresh/restart. Inactive unusable ignore name types do not refuse; explicit unresolved names only warn. |
| Invalid/unknown hash/derivation configuration | `content_splitter.py:SplitterConfig`; exact upstream algorithms and migration in `C:string_and_binary/chunks.ts:13-72,350-432,527-545`. Unknown splitter version normalization follows upstream; unsupported identity/hash combinations refuse rather than invent IDs. | Correct the named vault settings on devices and refresh. No extra finite cap on scalar coercion after JSON admission. |
| Proven splitter loops; V2 binary data loss | `content_splitter.py` guards measured nontermination or actual discarded bytes; upstream loop/addition boundaries in `C:string_and_binary/chunks.ts:224-252,350-432`. | Use positive piece size and a positive V2 plain minimum as the typed reason directs. Empty inputs, terminating zero/negative cells and V3 retain native outcomes. [Cell boundaries](vault-format.md#invalid-writer-parameters). |
| True chunk/path collisions, shared-leaf replacement | Both backends preflight IDs and data; upstream computes content IDs in `C:string_and_binary/chunks.ts:13-72`. Refusing protects another document sharing the ID. | Resolve the conflicting leaf in Obsidian or coordinate a different hash/positive chunk size. Journal compares TextEncoder-projected bytes; equal projected data is not a false collision. |
| Invalid UTF-8 plain attachment input; surrogate/Blob/BOM transformations | Plain paths require text; binary paths preserve bytes. Device splitting/Blob conversion may remove BOM or replace invalid code units (`C:string_and_binary/chunks.ts:224-252,350-432`; journal encoding `C:replication/journal/journalRecords.ts:69-77`). | Use a binary attachment path or explicitly opt into `lossy_text=true`; inspect typed publication notices. CouchDB 3.5.3 rejects lone-surrogate JSON: peer preflight refuses without leaf publication; restore raw lone-unit leaves from a healthy device or supply valid text. Valid astral cuts are protected automatically. |
| Compression markers/corruption | Upstream compression interprets reserved markers (`C:pouchdb/compress.ts:9-71`); peer reads them and refuses malformed compressed data or a lossy publication collision. | Restore corrupt content from a healthy device/backup; use a binary attachment path to preserve literal prefix bytes. CR/LF and BOM-preserving writes deliberately stay uncompressed. [Compression](vault-format.md#couchdb-compression). |
| Note/path/revision preconditions; excluded/oversized rename | Existing/missing note, stale revision, hidden/service namespace and corrupt chunk/reference checks prevent overwrite or protocol damage. Plugin move admission may delete the synced source after moving its retained disk file (`C:managers/StorageEventManager/admission.ts:78-108,124-130`). | Re-read latest revision, choose intended overwrite/create operation, admitted path/size and ordinary namespace. Peer refuses a dangerous rename because it has no destination disk copy. Service tools are out of scope, not fundamentally impossible. |
| Remote wipe/epoch/identity changes; unavailable storage; ambiguous publication | Peer retains local revisions/chunks, quarantines incompatible identity state, and does not silently retry uncertain writes. Upstream journal sync parameters/control flow: `C:replication/journal/JournalSyncCore.ts:168-229,895-920`. | Refresh/rebuild as guided; inspect local-only survivors; re-read affected paths after outcome unknown. Never reset state or republish survivors without an explicit retention decision. |
| Custom HTTP header/JWT/endpoint validation | Native Fetch ByteString/HTTP and WebCrypto import/signing semantics (`C:common/utils.ts:459-469`, `C:replication/httplib.ts:84-101,166-196`); SDK endpoint rules apply. | Correct the named variable/entry or server verifier. JWT wrong-key/expired tokens yield typed authorization guidance, not raw remote errors. Keep all credentials local. |

Constants for hash masks, UTF-8/UTF-16 ranges, Rabin windows, base64 expansion,
raw-deflate level/window, journal separators and version identifiers are codec
semantics rather than resource caps. They remain tied to native generator
fixtures; see [regeneration/provenance](regenerating-oracle-fixtures.md). Zero
counters, retry bookkeeping, private directory mode `0700` and scan chunk sizes
must not be counted as extra vault admission settings.


## Settings and behavior reference

## Supported vault settings

“Identical” means source-audited behavior at the pin, not live acceptance of every
combination. [Source inventory and caveats](superpowers/specs/2026-11-23-settings-parity.md);
[real-client validation scope](upstream-pin.md).

| Setting | Plugin values | Status | Peer behavior |
|---|---|---|---|
| `couchDB_CustomHeaders` | Key: Value lines | Transport-safety difference | Configure COUCHDB_CUSTOM_HEADERS locally; all database requests, origin restricted. Refuse transport-owned headers even when syntactically valid; force Accept-Encoding: identity |
| `bucketCustomHeaders` | Key: Value lines | Transport-safety difference | Configure S3_CUSTOM_HEADERS locally; final wire overrides, including signed headers, except transport-owned headers refused even when syntactically valid |
| `forcePathStyle` | Boolean, default true | Identical for configured endpoints | S3_FORCE_PATH_STYLE=true/false; SDK addressing rules still apply |
| `pathNormalizationVersion` | 0 legacy / 1 NFC | Identical | Both, independently of filename case; collisions refuse |
| `handleFilenameCaseSensitive` | false / true | Identical | Legacy, NFC, case-sensitive and NFC+case-sensitive identities |
| `chunkSplitterVersion` | v1, v2, v2-segmenter, v3-rabin-karp, legacy empty | Lossless text boundary exception | All writers; V1 non-plain and plain zero-cap cuts extend one UTF-16 unit and reset the next nominal size; moved pieces get new IDs; safe cuts, binary splits and reads are identical; journal collisions compare projected bytes; v2-segmenter equals v2 at this pin |
| `enableChunkSplitterV2`, `useSegmenter` | false / true (legacy migration) | Identical | One-pass migration of empty version; unknown version normalization follows upstream |
| `hashAlg` | xxhash64, xxhash32, mixed-purejs, sha1, empty | Identical | Upstream UTF-16 length and hash semantics |
| `customChunkSize` | JSON value; number >=0 in UI (default 0) | Device coercion; loops/data loss refuse | JavaScript truthiness/ToNumber coercion; V1 plain cap zero advances one UTF-16 unit; V3 clamps; see [loop refusals](vault-format.md#invalid-writer-parameters) |
| `minimumChunkSize` | JSON value; numeric in UI (default 20) | Identical wherever the plugin terminates | Original JSON values retain JavaScript ToPrimitive/addition (including array/string concatenation); negative, empty, null and NaN follow upstream coercion; only V2 nonempty plain minima whose addition preserves positive zero loop |
| `syncMaxSizeInMB` | Any JSON type; iterative JavaScript numeric coercion; default 50 | Identical | Advertising device policy before PREFERRED; device write admission and automatic text access budget resolution; textual files remain scan-eligible independent of extension; aggregate caps default unlimited; oversized rename refuses to protect source |
| `syncOnlyRegEx`, `syncIgnoreRegEx` | ECMAScript pattern lists | Identical when configured | Set local env explicitly; never inferred from milestone |
| `useIgnoreFiles` | Any JSON type; JavaScript truthiness | Identical | Derived from advertising device maps before PREFERRED unless overridden; active policy requires Node.js 22+ |
| `ignoreFiles` | Filename list | Identical | Derived from advertising device maps before PREFERRED unless overridden; exact comma split/JS trim; missing or unusable names add no rules; explicit unresolved names warn |
| `useEden` and count/length/age controls | Boolean / numbers | Identical | Current upstream writes empty Eden regardless; historical inline chunks readable |
| `longLineThreshold` | Numeric (default 250) | Identical | Unused by dispatched algorithms at this pin |
| `encrypt`, `E2EEAlgorithm`, `useDynamicIterationCount`, `encryptInternalMetadata` | E2EE options | Not supported for E2EE | Encrypted vaults fail startup; no encryption/decryption |
| `usePathObfuscation` | false / true | Not supported when true | Startup refuses; see issue #4 |
| `idDerivationVersion`, `idDerivationKey` | 0 legacy / 1 keyed | Identical for unencrypted vaults | Validate version/key configuration; unencrypted IDs follow upstream |
| `useJWT`, `jwtAlgorithm`, `jwtKey`, `jwtKid`, `jwtSub`, `jwtExpDuration` | HS256 / HS512 / ES256 / ES512 | Identical JWT wire format | Explicit connection env; zero duration uses five minutes; per-backend cached Bearer tokens, raw EC signatures; [setup](connecting-storage.md#couchdb-jwt-authentication) |
| `enableCompression` | false / true | Identical | CouchDB always reads compressed data; writes follow the vault (absent: false), verified on real CouchDB. Non-lossy CR/LF, surrogate and BOM exceptions below; reserved literal prefixes disclose decoded reads or refuse invalid input; journal forces off |
| `deleteMetadataOfDeletedFiles` | Boolean, default false | Deliberate difference | Always publish `deleted: true` soft deletion, never `_deleted`; devices honour either deletion marker; retain body/chunk references |
| `automaticallyDeleteMetadataOfDeletedFiles` | Days, default 0 (disabled) | Deliberate difference | Never expire or purge deletion history; device-side maintenance remains separate |
| `disableMarkdownAutoMerge` | Boolean | Deliberate difference | No automatic Markdown merge; CouchDB mutations refuse unresolved siblings; journal applies PouchDB's winner rule |
| `resolveConflictsByNewerFile` | Boolean | Deliberate difference | No mtime-based conflict resolver; resolve in Obsidian before editing a conflicted CouchDB note |
| `writeDocumentsIfConflicted` | Boolean, default false | Deliberate difference | Device reflection policy is not applied; journal reads the revision a device's database selects, CouchDB writes still refuse unresolved siblings |
| `processSizeMismatchedFiles` | Boolean, default false | Device filesystem only | Controls device reflection when metadata and reconstructed Blob size differ; the peer reads storage directly and discloses its own lossy writes |
| `doNotDeleteFolder`, `trashInsteadDelete` | Boolean | Device filesystem only | No filesystem folders or trash on the peer; soft deletion is independent of the device's trash choice |
| `doNotUseFixedRevisionForChunks` | Retained Boolean | No active consumer at this pin | No active chunk-manager/database implementation reads this setting; not an additional peer tuning switch |
| `suspendParseReplicationResult` | false / true | Deliberate difference | Reads remain available from DB; no filesystem reflection stage |
| Hidden File Sync / customization sync | `i:`, `ix:` namespaces | Not supported | No local vault folder; hidden ignore files can be read internally |
| Plugin sync / `usePluginSyncV2` | `ps:` namespace / Boolean | Not supported | Service documents excluded from ordinary tools |
| Tweak mismatch/auto-accept flags | Boolean | Deliberate difference | Local env policy; no device registration or PREFERRED publication |
| YAML anchors / aliases | Note bytes, parsed by host tools | Properties supported | Memoised projection; [expanded-node/byte/depth budgets](frontmatter-properties.md) affect only properties/tags |
| Transport deadlines / SDK attempts | Device-local configuration | Local overrides | See environment table; unconditional S3 publication always single attempt |
| Reflection/request/cache/concurrency tuning | Various local device options | Not supported as device settings | No host reflection or plugin request engine; [full inventory](superpowers/specs/2026-11-23-settings-parity.md#ж-other-tweak_values-and-production-evidence-boundary) |

Real-server coverage includes the 60-row pairwise matrix on both backends.
It does not establish every setting combination on every server.

## How the peer differs from a device

Properties/tags resolve YAML anchors and aliases, including reused containers.
Untouched top-level key text survives edits byte-for-byte. A streaming preflight
bounds expanded nodes (default 20000) and expanded UTF-8 string/key bytes (4 MiB) and nesting depth (200), all adjustable
with the environment table. Cycles have no finite JSON representation. Typed
refusals leave the note intact and synced and direct callers to fetch/edit raw
note text. These are property-tool budgets, not file sync limits;
[details and recovery](frontmatter-properties.md).

Transport defaults remain 5 s connect / 30 s I/O and 5 s selection requests;
all are configurable in the environment table. CouchDB pool waits now use the
I/O value. S3 defaults to one SDK attempt to preserve ambiguous-write reporting;
set S3_MAX_ATTEMPTS=4 for upstream's attempt count on reads and conditional
writes. Unconditional fallback PUT always uses one attempt, even then. See
[retry and lost-response semantics](s3-journal.md#transport-policy).

[Dated acceptance corrections](upstream-pin.md#2026-11-25-acceptance-corrections).

The peer extends a legacy text cut one UTF-16 unit to keep a valid surrogate
pair together, then resets the next piece to its full nominal size. This applies
to V1 non-plain text and V1 plain zero caps; all other safe partitions/IDs remain
identical. Binary cuts and reads are unchanged. There is no switch.
CouchDB 3.5.3 rejects lone-surrogate leaves before publication; journal TextEncoder
still replaces lone units with U+FFFD and reports `transport_replacements`.
Caller-provided lone units retain Blob ingress replacement with
`input_surrogates_replaced` on both backends. See [lossless boundaries](vault-format.md#lossless-text-boundaries).

- CouchDB auth is explicit local configuration: choose Basic credentials or the
  JWT environment group, independently of MCP client auth. JWT requires PKCS8 EC keys for ES256/ES512 (SEC1 is rejected);
  see [key generation](connecting-storage.md#couchdb-jwt-authentication).
  JWT matches the pinned plugin, including original subject whitespace and Latin-1 JSON encoding.
  Non-Latin-1 identifiers cannot be encoded by the plugin either; configure
  matching Latin-1 identifiers on the server and device. Vault stats expose only
  `auth_mode` (`basic` or `jwt`) and `jwt_algorithm`, never keys or subjects.
  JWT startup verifies `/_session` returns the configured subject. Failed tokens
  receive typed authorization guidance for key, algorithm, key ID, subject and
  clock. A later HTTP 400 is an auth error only if the same token fails this
  session check; ordinary bad requests retain their original classification.

- CouchDB compression uses Python zlib raw deflate level 8; bytes may differ
  from device fflate output, but devices read either stream. The peer preserves
  CR/LF text instead of treating line-wrapped base64 as binary (upstream loses
  those line breaks). It publishes data containing
  a leading BOM uncompressed to prevent Blob conversion/decoding losses.
  Reads of existing compressed text match device Blob.text(), including U+FFFD
  replacement and BOM removal. Invalid base64/deflate refuses with a sanitized
  error: restore the document from a healthy device or backup. Literal reserved
  compression prefixes in new text pieces retain device decoding with typed
  notices; undecodable prefixes refuse before publication. Use a binary
  attachment path to preserve literal prefix bytes. See
  [compressed document format](vault-format.md#couchdb-compression).

- JWT expiry is numeric: omit it or set `0` for five minutes; an explicit empty value
  is refused. Upstream literal JSON string `"0"` means zero seconds and cannot be
  represented by this numeric environment setting.
- Existing conflicting shared journal leaves refuse before publication. Inspect and
  resolve the conflicting leaf in Obsidian, or coordinate an alternate hashAlg
  or a positive customChunkSize before retrying.
- E2EE and obfuscated paths are unsupported; use an unencrypted, unobfuscated
  vault until [#3](https://github.com/adeepn/obsidian-livesync-mcp/issues/3) /
  [#4](https://github.com/adeepn/obsidian-livesync-mcp/issues/4) are implemented.
- Deletion always publishes a soft `deleted: true` revision, retaining body and
  chunk references; the peer never purges deletion history. Devices honour this
  marker even when configured to publish `_deleted` themselves. The peer does
  not implement `deleteMetadataOfDeletedFiles` or timed
  `automaticallyDeleteMetadataOfDeletedFiles` cleanup. Run any intentional
  history cleanup separately through LiveSync after agreeing retention.
- No automatic conflict resolution runs: `disableMarkdownAutoMerge`,
  `resolveConflictsByNewerFile` and `writeDocumentsIfConflicted` do not change
  peer conflict handling. CouchDB refuses mutations of notes with unresolved
  sibling revisions; journal reads/writes use the revision a device's database
  selects (PouchDB's winner rule) while retaining siblings. Resolve conflicts in
  Obsidian before retrying
  a refused edit; a successful journal write is not proof that another branch
  was merged. [Source references](peer-limitations.md#deletion-and-conflict-policies).
- Replay has operational byte/count/depth/time budgets. Named
  `ReplayBudgetExceeded` errors identify the env override to raise; Python JSON
  decoder capacity is a separate ceiling. [Budget defaults and recovery](size-policy.md).
- Rename into an excluded or oversized destination refuses before mutation:
  the peer has no disk copy at the destination and must protect the only source.
  Choose an admitted path/size. Invalid UTF-8 on plain paths also refuses unless
  attachment upload explicitly opts into `lossy_text=true` replacement.
- Remote wipe recovery retains local revisions/chunks. Local-only survivors stay
  readable and are not republished automatically; account for them before reset.
- Vault-derived scalar settings use JavaScript coercion: numeric strings and arrays
  can limit size; `true` means 1 MiB, while null, false and nonnumeric values
  disable size admission. Ignore enablement uses truthiness, including truthy
  empty arrays/objects and strings. Operator overrides keep strict validation.
  Array conversion is iterative; it adds no coercion depth limit after JSON admission.
  Cyclic Python arrays use native empty-field cycle handling.
  Device maps refuse when effective policies disagree without a decidable winner;
  an object whose own toString prevents numeric conversion of `syncMaxSizeInMB`
  needs correction in the vault or `LIVESYNC_SYNC_MAX_SIZE_MB`.
- Ignore-file selection derives from advertising device maps before PREFERRED unless independently overridden.
  Active selection requires Node.js 22+, including vault useIgnoreFiles=true.
  LIVESYNC_USE_IGNORE_FILES=false is allowed, but devices drop peer writes to
  ignored paths. Reads bypass all filters, including explicitly configured filters;
  devices retain local files that later become ignored, and the database remains
  the read source of truth. With filters off no Node is launched.
  Name-shape validation is never fatal; unusable/missing names contribute no rules.
  Policy arbitration is separate: if equally newest devices disagree without an
  applicable PREFERRED value, a typed ambiguity refusal names
  `LIVESYNC_IGNORE_FILES` / `LIVESYNC_USE_IGNORE_FILES`; set the named override
  or align devices/PREFERRED and restart. Inactive name types remain unvalidated.
  `selection_unresolved_ignore_names` counts explicitly configured names not resolved
  by the latest root/ancestor lookup. Inactive ignore settings are not validated.
  [Filter syntax](selection-filters.md).
- Hidden File Sync, customization/plugin sync and suspended filesystem reflection
  are outside the ordinary-tool scope. The peer has no local vault folder; this
  is a scope choice, not a claim that service namespaces cannot have APIs.
  DB reads include received oversized files;
  a device may decline to reflect those files until its settings permit them.
  `processSizeMismatchedFiles` controls device size-mismatch reflection;
  `doNotDeleteFolder` and `trashInsteadDelete` affect only its filesystem.
  Plugin encrypted setup exports are not supported; copy
  connection fields and choose CouchDB Basic or JWT authentication.
  Ordinary tools reserve service prefixes/names and dot-prefixed paths;
  [path admission](vault-format.md#path-admission) lists the exact exclusions.
- Successful writes disclose lossy ingestion through typed publication notices: legacy BOM removal, invalid UTF-8 replacement, Blob input surrogates and journal wire surrogates. The original tool result remains verbatim, followed by a separate notice record and fixed prose. BOM size mismatches prevent default device reflection; see [typed notices](outcome-reporting.md#typed-publication-notices).
- Distinct colliding CouchDB pieces refuse before publication: configure a positive
  `customChunkSize` or a collision-free `hashAlg` such as `sha1`. Journal pieces
  with identical TextEncoder bytes share the original ID and report replacements.
- Refuse inputs on which the plugin itself does not terminate: V1
  nonempty binary/non-plain text with nonpositive caps, V1 nonempty plain text
  with negative caps, V2 nonempty binary with nonpositive caps, V2 text groups
  containing more than one Unicode code point with nonpositive caps (or empty
  groups with negative caps), and V2 nonempty decoded plain text with
  `minimumChunkSize` whose first JavaScript addition preserves positive zero (ordinary 0, empty string, null, or zero-only/whitespace-only primitive strings);
  negative zero and zero strings whose concatenation becomes NaN terminate.
  The terminating NaN-cap V2 binary case also refuses: upstream slice(i, NaN)
  discards the input bytes. This safety refusal protects data; set a numeric
  `customChunkSize` such as 0. For loops, use `customChunkSize` producing a
  positive pieceSize and a positive V2 plain `minimumChunkSize`.
  Empty inputs and V3 retain upstream semantics.
  [Exact cell evidence and recovery](vault-format.md#invalid-writer-parameters).
- The peer is an invisible writer: it does not register a device or modify
  milestone/PREFERRED settings. Align device settings separately; review
  [tweak compatibility](tweak-compatibility.md) after upgrades.
- All text access uses one UTF-8 byte budget for plain documents and textual
  newnotes: unset resolves to max(positive vault size policy in bytes, 256 MiB),
  explicit `LIVESYNC_TEXT_SCAN_MAX_BYTES` wins, and 0 is unlimited. Newnotes
  additionally require strict UTF-8/control validation. Beyond-budget fetches
  explain the refusal and edits preserve the revision; byte download is available.
  Scans report skipped paths and have no default note/byte cap. Each call has a
  20-second time budget (0 disables it), including scan-slot waiting; a busy
  refusal has reason `scans_busy` and retry guidance. Optional operator caps and
  response limits produce `truncated: true`, `next_cursor`, and a `scan` object
  with notes, bytes, `elapsed_ms`, configured maxima and `stopped_because`.
  Pass the opaque `cursor` to continue strictly after the last processed display
  path; paths created or renamed behind it between pages can be missed or
  repeated. The CouchDB metadata index resumes by database update sequence and
  explicitly reports incomplete state. Search defaults to 200 matching notes;
  other growing responses page by 200 source notes. List responses use a
  `results` envelope so even complete responses can carry `scan` metadata.
  Fetch accepts inclusive `start_line` / `end_line` windows after full text
  validation. Regex uses one persistent isolated worker; replacement output
  defaults to the effective text budget and request deadlines default to
  10 seconds. [Tool matrix and scan policy](scan-policy.md).
- Custom request headers are local connection credentials, copied from the plugin,
  not inferred from shared vault settings. Invalid HTTP names/control characters
  refuse startup with variable, entry ordinal and rule; correct that entry.
  Syntactically valid transport-owned headers are also refused: Host,
  Content-Length, Transfer-Encoding, Connection, Keep-Alive, TE, Trailer, Upgrade
  and all Proxy-* fields. This is a transport-safety difference from the plugin;
  startup errors are value-free. Remove these entries and configure routing/proxy
  transport separately. Text form follows upstream colon truncation; use JSON
  for colon-containing values.
  Transport library logs are suppressed process-wide once custom headers are used;
  application diagnostics report typed presence/count only.
- S3 custom endpoints default to path-style addressing; virtual-host-only providers
  must set S3_FORCE_PATH_STYLE=false. Publication has an exists+put race fallback on stores
  without conditional writes. [S3 operation](s3-journal.md).

The [complete limit and refusal inventory](peer-limitations.md) also covers
configured transport/selection deadlines, YAML property-conversion bounds and SQLite
capacity. These are resource/tool constraints, not LiveSync vault settings.

## Outcome reporting

**Applied** confirms durable publication, not conflict winner status.
**Refused without change** leaves the requested mutation unapplied;
**skipped** means the new oversized input was not saved and any old revision
remains. **Outcome unknown** means metadata publication may have happened:
re-read affected paths before retrying, especially non-idempotent edits.
[Operation/retry contract and journal reconciliation](outcome-reporting.md).

## Size behavior

Ordinary writes skip only if `max > 0 && size > 0 && size > max*1024*1024`.
Equality is admitted; fractions work; <=0 disables. Default is 50 MiB from
vault settings, unless explicitly overridden. Raw and converted Blob bytes are
checked before publication. Downloads have no device-size gate; `max_bytes`
is an optional caller limit. Journal refresh validates the complete selection/size/text candidate before committing any effective setting; refused candidates preserve the previous tuple and block writes until repaired. Journal refresh re-resolves the effective vault size policy and derived text budget; explicit overrides remain authoritative. CouchDB re-resolves at startup.
[Full size policy, skips, protected renames and independent replay budgets](size-policy.md).

## Local state and recovery (S3)

Persist a dedicated `S3_STATE_DIR` per peer/vault. `journal.sqlite3` with its
WAL/SHM sidecars forms a rebuildable replica with history, not an eviction
cache; disk can exceed vault size. `chunks/` holds files a release before chunk
documents wrote; they are read for a chunk no document names and never written
again. Do not share it between processes.
Wipe clears replication tracking only; an epoch change alone preserves dedupe.
Path identity changes archive the whole old directory under
`<state_dir>.quarantine/<UTC timestamp and unique suffix>/state`, then rebuild.
To restore, stop the peer, move current state aside, restore the archived
`state` and the old identity settings. Monitor `obsidian_vault_stats.local_state`.
[Safe reset, survivors, disk warnings and full restoration steps](local-state-and-recovery.md).

## Upstream tracking

[Pin and source/live evidence](upstream-pin.md). The daily/manual watcher
checks plugin releases and commonlib protocol-manifest changes, maintaining
one `upstream-drift` issue; failures do not close alerts. Run
`scripts/check_upstream.sh` with authenticated `gh`. Review drift, extend tests
and update the pin by PR. Monthly scratch-bucket smoke:
[existing checklist](live-check-s3-checklist.md); settings parity acceptance:
[owner checklist](live-check-parity-checklist.md).

## Security, systemd, clients and authentication

Keep credentials and `.env` out of version control. Default logs/diagnostics
omit remote bodies, headers, secrets and settings values; note paths remain
visible with control characters escaped. Reporting and hosting boundaries log only
cause classes and typed fields, without exception chains. Keep unsafe debug disabled.
[Diagnostic policy](diagnostics-and-secrets.md).

Use one auth mode: bearer `MCP_API_KEY`, or the complete OAuth group.
Bearer takes precedence if both are supplied. OAuth uses password + PKCE,
pre-registered Claude callbacks and resource binding to the configured URL
plus `/mcp`; DCR is disabled. [Systemd commands, Claude.ai setup and other clients](clients-and-authentication.md).

### Text access tool matrix

| Tools | Within effective text budget | Beyond effective text budget |
|---|---|---|
| `obsidian_fetch` | Whole text, properties and optional stats | Metadata, `content_available: false`, `size_bytes`, `budget_bytes`, `reason: text_budget_exceeded`, remedy naming `LIVESYNC_TEXT_SCAN_MAX_BYTES` |
| `obsidian_update_note` (all modes), properties, tags, canvas, templates | Whole-text operation with existing type/path rules | Typed `TextBudgetExceeded` refusal with the same fields and remedy; no write |
| Search, tags, links, graph, statistics | Whole eligible text subject to aggregate scan limits | Path in `skipped`; metadata still counts against aggregate limits |
| `obsidian_download_attachment` | Existing binary/newnote byte download rules; plain text uses fetch | Complete bytes for either storage type, subject to caller `max_bytes` |

`obsidian_vault_stats` exposes `text_budget_bytes`, `regex_max_output_bytes`
and `regex_timeout_seconds`. Zero bytes means unlimited. Explicit regex output
limits are independent of the input text budget. Regex size and deadline
refusals name `LIVESYNC_REGEX_MAX_OUTPUT_BYTES` and
`LIVESYNC_REGEX_TIMEOUT_SECONDS` respectively. Literal replacement has no
regex output cap. Device write-size admission still applies after edits.




Journal candidate validation includes the Node selection capability check. While that check awaits, effective selection, size and text limits remain the previous tuple; a missing/unusable Node refusal records a typed failure without committing the candidate. Install Node.js 22+ in the service PATH, then force refresh or restart to recover.

After a failed candidate, force refresh also revalidates a restored previously active policy, so recovered backend/vault stats clear stale Node capability failures before another write.

HTTP ingress: `LIVESYNC_MAX_REQUEST_BYTES` defaults to 1 GiB; 0 is unlimited.
HTTP 413 means raise the request-body policy (and any proxy limit) and restart.
The body is held in memory; this is separate from device file-size admission
and the OAuth form cap. See [configuration](configuration.md#http-request-bodies).

CouchDB inflate protection (P-4): per-document resolved splitter budget, 32 MiB
floor, positive LIVESYNC_COUCHDB_MAX_INFLATED_BYTES override. Raise and restart
for larger valid pieces or HTTP responses. HTTP-level decoding uses bounded wire
and decoded bytes before JSON; document inflation is bounded separately. This
operational protection is separate from device file-size admission. [Derivation](vault-format.md#couchdb-inflate-budget).

Custom headers deliberately differ from the plugin for transport safety:
Content-Length, Transfer-Encoding, Host, Connection, Keep-Alive, TE, Trailer,
Upgrade and all Proxy-* fields are transport-owned and refused case-insensitively
for both storages. Remove these entries and configure routing/proxy transport
separately. Other headers still apply after S3 signing.

CouchDB response outcomes use an explicit endpoint-purpose table: read-only POST
`_all_docs`/`_changes` refusals carry no publication uncertainty. Undeclared
operations refuse before sending with upgrade guidance; write and orphan-leaf
recovery is unchanged. [Endpoint inventory](vault-format.md#unreadable-write-acknowledgements).

## Recommendation-only writer policy

`syncMaxSizeInMB`, `useIgnoreFiles` and `ignoreFiles` follow the devices, not PREFERRED.
Devices that agree give their common value without any timestamp, and a key that nobody
advertises takes its device default. Only an unresolved disagreement makes the peer
refuse: the devices disagree and neither the newest device, agreeing equally newest
devices nor PREFERRED decides. The refusal is typed and names the operator override:
`LIVESYNC_SYNC_MAX_SIZE_MB`, `LIVESYNC_USE_IGNORE_FILES` or `LIVESYNC_IGNORE_FILES`.
The resolution order and the tie rule are defined once in
[Tweak compatibility](tweak-compatibility.md#recommendation-only-writer-policy).
