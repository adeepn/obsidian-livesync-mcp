# Settings parity: upstream behavior extraction

> **Part 0 round 2 — 2026-11-24 (S1, P-27/P-32).** Production splitting is
> unchanged. `tests/splitter_comparisons.json` is a committed regression
> inventory, checked against every AST comparison operator (chained endpoints
> separately). Each witnessed equality is mutated in an isolated namespace and
> compared with executed upstream expectations; every other entry carries a
> cited unreachable, indistinguishable or admission proof. The zero-minimum
> guard remains explicitly owned by G3. No recipe-family count substitutes for
> observability. Direct helper-level oracle vectors execute published wheels'
> short UTF-16 encoder and the pinned raw-hash manager conversion at zero,
> signed-32 and Unicode boundaries, with splitter-call reachability checks.
> Malformed programmatic text/plain Blobs cover decoder lengths 126–129 and
> continuation/lead/scalar cases; fractional V2 ratios surround 100 at two
> minima. G3's V1 non-plain cut is published on both backends: CouchDB preserves
> escaped units, journal leaves use the separately executed surrogate-wire
> fixture's U+FFFD projection and retain upstream IDs. V2 protects pairs.
> The loader validates recipes before exporting ORACLE/GRID/MATRIX and names
> the regeneration command on failure. The 53 known mutations, G1–G4 and new
> H1–H4 (decoder continuation, ASCII boundary, U+10000, V1 fence EOF) have a
> reproducible exact-node campaign in `scripts/check_splitter_mutations.py`.
> Run commands and fixture locations are recorded in AGENTS.md. This is
> offline regression evidence, not a new live-device acceptance claim.

> **Implementation status — 2026-11-24 (Task 7).** The extraction tables below
> preserve the Task 1 starting-code snapshot and proposed actions; “our current
> implementation” in those historical rows is not a statement about today's
> branch. Tasks 2–6 implemented identity, splitter/hash/migration, ingestion,
> size, selection and compatibility parity. Current supported/unsupported and
> deliberate-difference statuses are consolidated in the
> [README settings table](../../../README.md#supported-vault-settings), with
> local recovery and outcome contracts linked there. E2EE/path obfuscation and
> filesystem/internal/plugin sync remain outside supported scope. CouchDB
> compression and JWT were subsequently implemented (see dated G6/G7 notes).
> Full-suite selection integration tests do need Node; only
> committed-oracle tests are Node-free. Plugin encrypted setup exports are not
> the peer's `sls://` connection format. Live parity acceptance is pending Task 8.


> Historical baseline cap references and the original Task 5 size/replay
> assumptions below are superseded by the Task 5 fix-round-1 contracts in
> spec §д: vault size policy, separate raw/Blob gates, P-13 rename refusal,
> independent replay budgets and explicitly reported Python decoder capacity.

Audited upstream commits: commonlib **484c164450d61deb2e301582b60cb9b5f9fa977f**; plugin **5070b67beae3b60e3a79132386a08da3f1ef213a**.

[DECISION] Research for Task 1 only; no implementation changes. This document overrides **assumptions** in Tasks 2–5 where the source disagrees. Task numbers below refer to the actual plan headings: Task 2 identity, Task 3 splitter/hash, Task 4 types, Task 5 size policy (some review-focus text numbers them differently).

## Evidence and scope

`U:` means a file under `src/` in `vrtmrz/livesync-commonlib` at **484c164450d61deb2e301582b60cb9b5f9fa977f** (package 0.7.0). Immutable source root: <https://github.com/vrtmrz/livesync-commonlib/tree/484c164450d61deb2e301582b60cb9b5f9fa977f/src>. `P:` means `vrtmrz/obsidian-livesync` **5070b67beae3b60e3a79132386a08da3f1ef213a**, tag **1.0.52**, under `src/`; source root: <https://github.com/vrtmrz/obsidian-livesync/tree/5070b67beae3b60e3a79132386a08da3f1ef213a/src>. Unprefixed paths are this repository at starting HEAD `660b33e`.

The requested `/tmp/ls-commonlib` was an incomplete directory (no Git metadata, almost no source). A fresh clone was read at `/tmp/settings-parity-commonlib`; it resolves to the same commonlib commit already recorded in `docs/upstream-pin.md:1-9`. Supplemental plugin source was read at `/tmp/settings-parity-plugin`. Neither source checkout was edited or executed. The pin document still names plugin 1.0.50/live acceptance; this extraction audits 1.0.52 source, **not** live validation of that version. Updating the pin is Task 7.

The trace is storage/vault → file selection → typed Blob → EntryManager → splitter → hash → local DB → journal pack; receive is journal decode → local DB persistence → replication-result policy → vault reflection. A low-level save API may bypass caller policy; a guard in one layer must not be claimed as a universal database constraint.

## а. ContentSplitter v3

Definitions used below: `N` = Blob byte size; `m` = minimumChunkSize; `A = floor(102400 × ((customChunkSize || 0) × 1 + 1))`; `E = max(A, 30720)`. All boundary sizes are **bytes**, not Python character counts. All string lengths used for IDs are separately JS UTF-16 lengths.

| Question | Exact upstream behavior + citation | Our current implementation + citation | Delta / Task 2–5 action |
|---|---|---|---|
| Which v3 implementation runs? | `chunkSplitterVersion = "v3-rabin-karp"` selects ContentSplitterRabinKarp; worker/direct paths invoke the same split algorithm. `U:common/models/setting.const.ts:62-68`; `U:ContentSplitter/ContentSplitterRabinKarp.ts:9-20`; `U:worker/bgWorker.direct.ts:63-80`. | One whole-content chunk, no splitter selection: `storage_journal.py:1036-1053`; `storage_couchdb.py:955-960`. | Task 3 must select the actual algorithm from settings. There is no generic boolean “splitter off” mode; older versions have their own algorithms. |
| customChunkSize / plainSplit input | Base derives `A` above from MAX_DOC_SIZE_BIN=102400. `plainSplit` initially comes from path suffix `.md`, `.txt`, `.canvas`, case-sensitive; any `text/plain` Blob also enables text thresholds inside v3. `U:ContentSplitter/ContentSplitterBase.ts:65-94`; `U:common/models/shared.const.behabiour.ts:4-5`; `U:string_and_binary/path.ts:176-180`; `U:string_and_binary/chunks.ts:504-508`. | No settings-derived piece size: same writer references above; binary whole-file encoding `livesync_format.py:354-361`. | Task 3 must preserve the distinction between path hint, Blob type, thresholds, and output encoding. Even a binary Blob with `.md` path can get text thresholds but still base64 output. |
| Small TEXT: window, average/min/max | For initial text mode and `N < 4 MiB`, start `u=64`. Repeatedly evaluate `N/(4u)`; when >500 increase `u` by 32, repeat until ≤500. `M=min(16u,E)`; `L=min(max(2u,m),M)`; `Q=min(max(4u,L),M)` is the **hash modulus**, not an exact measured mean. With small input, m=20, custom=0: L=128, Q=256, M=1024 bytes. `U:string_and_binary/chunks.ts:504-542`. | Whole text is one chunk: `storage_journal.py:1036-1053`; `storage_couchdb.py:955`. | Task 3 port the adaptive `u` loop, not a fixed 1 KiB splitter. Minimum is a candidate threshold; final tail can be shorter. |
| Large TEXT / BINARY thresholds | Text mode is switched off at **N ≥ 4×1024²**, but Blob type is retained. Non-text thresholds: binary unit=256 KiB; `M=min(4 MiB,E)`; `G=max(1,floor(M/4))`; `L=min(max(G,m),M)`; `Q=G` (even if m raises L). At custom=0: M=102400, L=25600, Q=25600 for m=20. `U:string_and_binary/chunks.ts:509-542,571`. | Binary produces one arbitrarily large base64 piece: `livesync_format.py:354-361`; `storage_journal.py:1015-1017`. | Task 3 split raw bytes **before** base64. Do not use the 4 MiB algorithm cap as the default: custom=0 caps at 100 KiB. Large text uses these thresholds but remains text. |
| Exact rolling hash and boundary | Window 48, prime 31, outgoing weight=31^47 with Math.imul signed-32 overflow. Initially hash/window zero. Before consuming next byte, boundary if chunkSize≥M OR (chunkSize≥L AND unsigned32(hash) mod Q = 1); text additionally requires next byte not a UTF-8 continuation `(byte & 0xc0) != 0x80`. Update hash with `imul(hash - imul(oldByte,weight),31)+byte`, signed32. Rolling state is **not reset** when a chunk ends. Input blocks=1 MiB, previous 48 bytes retained across blocks. `U:string_and_binary/chunks.ts:544-588,603-639`. | No rolling algorithm: writer references above. | Task 3 preserve 32-bit arithmetic and window across both piece and read-block boundaries. For valid UTF-8 text, cuts can exceed M by up to 3 bytes; a strict M-byte text cap would be wrong. A manually supplied malformed text/plain Blob can contain longer continuation-byte runs: the guard checks byte pattern, not validity, so no universal M+3 bound follows for that ingress. |
| EOF, empty input, record encoding | Empty Blob emits no pieces. Binary boundary may finish at read-block end; final remainder is emitted. Text pieces are decoded individually with readString; **each binary piece independently base64-encoded**. `U:string_and_binary/chunks.ts:574-576,617-638`; `U:string_and_binary/convert.ts:20-30`. | Binary reader already decodes per piece, writer only encodes whole body: `livesync_format.py:337-361`. Current writer always creates a child, including empty text: `storage_journal.py:1036-1042`. | Task 3 emit no child for empty data, retain short EOF pieces and per-piece base64; do not base64 the whole file then split that string. |
| minimumChunkSize / longLineThreshold | m changes L and (text only) Q by formulas above; never raises M. `longLineThreshold` is present in tweak template but is **not passed** to the selected splitter and has no v3 effect. `U:common/models/tweak.definition.ts:25-26`; `U:ContentSplitter/ContentSplitterBase.ts:65-94`; `U:ContentSplitter/ContentSplitterRabinKarp.ts:17-20`. | Neither setting consumed by writers: `storage_journal.py:979-1053`. | Task 3 consume m; do not invent a long-line boundary rule from the setting name. |
| Piece → local chunk → journal record | EntryManager skips empty pieces, computes id, adds `{_id:id,data:piece,type:"leaf"}`, and keeps ordered child IDs including repeats; size is Blob.size. Metadata has children/_id/path/ctime/mtime/size/type/eden:{}; flush threshold ~2 MB only batches writes. Journal serializes chunk as UTF-8 `~h:ID␟escaped(data)\n`, metadata as JSON+newline. `U:managers/EntryManager/EntryManagerImpls.ts:83-149,252-282,371-388`; `U:replication/journal/journalRecords.ts:70-76`. | Matching compact serializer and metadata shape: `journal_codec.py:122-159`; `livesync_format.py:374-399`; only one child. | Task 3 replace piece preparation, preserve journal protocol. Task 5 distinguish piece limit, journal batch size and whole-file policy. |

[GOTCHA] Decoder parity (Task 3c fix round 1): V3 calls the published
`octagonal-wheels@0.1.57` `dist/binary/base64.js:249-298` `readString` through
`U:string_and_binary/convert.ts:2-17`. It preserves every BOM. Above 128 bytes
it uses nonfatal UTF-8 `TextDecoder(ignoreBOM=true)`; at <=128 bytes it manually
consumes unchecked continuation bytes (missing bytes coerce to zero, unknown
lead bytes disappear, `String.fromCharCode` truncates to 16 bits). It can emit
lone surrogates from malformed bytes. V1/V2 instead call `Blob.text()`
(`U:string_and_binary/chunks.ts:357,445`): remove the initial BOM once, replace
malformed UTF-8, preserve NUL. Blob construction from JS strings first replaces
lone surrogates; non-plain V1 may subsequently split a valid surrogate pair.
Oracles must execute the published decoder, verified against the pinned
lockfile SHA512 and tar members, rather than substitute TextDecoder.

## б. Chunk identifiers

| Question | Exact upstream behavior + citation | Our current implementation + citation | Delta / Task 2–5 action |
|---|---|---|---|
| Unencrypted xxhash64 formula | `id = "h:" + base36(xxhash64(UTF8(piece + "-" + decimal(JS_UTF16_length(piece)))))`, default h64 seed, unsigned bigint base36, lowercase, **no fixed width / no zero padding** (1–13 digits). The piece is already text or base64 from §а. `U:managers/HashManager/XXHashHashManager.ts:73-99`; `U:managers/EntryManager/EntryManagerImpls.ts:386-388`; `U:string_and_binary/hash.ts:1-2` (xxhashNew re-export). Upstream vector: `helloWorld` → `h:37me21jj44pcc`, `U:managers/HashManager/HashManager.unit.spec.ts:17-23,249-256`. | `h:` + first 16 hex of SHA256(UTF8(data)): `livesync_format.py:364-371`. | Task 3 replace SHA256; UTF-16 length for emoji, not Python len or byte count. Use the base36 vector, not the E2EE hex vectors. |
| idDerivationVersion with encrypt=false | configuredIdKey validates version/key even before encryption branch. Absent/0 with empty key is legacy; v1 requires 64 lowercase hex key; other versions/partial combinations throw. **Valid v1 key with encryption off still uses ordinary unkeyed hash**. `U:common/idDerivation.ts:53-67`; `U:managers/HashManager/HashManager.ts:110-119`; regression `U:managers/HashManager/HashManager.unit.spec.ts:116-141`. | Only identity/encryption/obfuscation/case keys recognized, no derivation validation: `livesync_format.py:145-176,238-257`. | Task 3 must not interpret version=1 alone as a new plaintext hash recipe. Validate settings consistently; no secret keys in diagnostics/artifacts. |
| E2EE legacy vs independent IDs (issue #3 context only) | Legacy encrypt: `h:+` + base36(h64(piece + "-" + hashedPassphrase + "-" + JS_length)); passphrase hash derived via SALT_OF_ID + first floor(3×passphrase.length/4) JS units and fallbackMixedHashEach. `U:managers/HashManager/HashManagerCore.ts:75-81`; `U:managers/HashManager/XXHashHashManager.ts:88-89`; `U:managers/HashManager/HashManager.ts:110-119`. Encrypt + v1: HMAC-SHA256 purpose-derived chunk key; input includes fixed context + **16-digit zero-padded hex xxhash64(piece)**, output 64 hex prefixed h:+. `U:common/idDerivation.ts:11-12,70-90`. | E2EE refused: `livesync_format.py:158-163,238-242`. | Out of implementation scope; keep refusal. The padded hex internal to keyed derivation must not leak into plaintext implementation. |
| Cached ID reuse / other hashAlg | Legacy prepareChunk first reuses cached id for identical data; independent-key mode instead checks computed id/data. HashManager selects by hashAlg, with fallback managers. `U:managers/EntryManager/EntryManagerImpls.ts:375-388`; `U:managers/HashManager/HashManager.ts:8-19,65-74`. | Existing deterministic SHA id and reuse checks: `storage_couchdb.py:790-840`; `storage_journal.py:1036-1037`. | Task 3 define vectors for **fresh** chunk preparation; old cache can reuse earlier IDs. Claiming any hashAlg parity after adding only xxhash64 is unsupported; other algorithms need faithful implementations or an explicit remaining gap. |

## в. plain / newnote

> **Historical baseline:** the "Our current implementation" column below
> records the starting revision. Its extension-based write refusals and
> `str`-only API are superseded by Task 4 and the fix-round-1 contracts below.

| Question | Exact upstream behavior + citation | Our current implementation + citation | Delta / Task 2–5 action |
|---|---|---|---|
| Device extension list / sniffing | `isPlainText` uses case-sensitive suffixes **.md .txt .svg .html .csv .css .js .xml .canvas**, no content sniff. FileAccessBase.vaultReadAuto/adapterReadAuto selects host text read for that list, binary read otherwise. readStubContent wraps the resulting string/ArrayBuffer with createBlob. `U:string_and_binary/path.ts:164-174`; `U:serviceModules/FileAccessBase.ts:162-167,201-206`; `U:serviceModules/ServiceFileAccessBase.ts:265-276`; `U:common/utils.ts:100-115`. | Read classification list + type plain override: `livesync_format.py:299-308`; write binary denylist: `livesync_format.py:311-325`. | Task 4 remove denylist as a device-type rule. `.MD`, `.json`, `.base`, `.png` are outside this exact list, even when contents look textual. |
| Final save type vs extension hint | Final EntryManager overrides supplied type from Blob MIME: **exactly text/plain → plain, everything else → newnote**. `U:common/utils.ts:97-98,301-302`; `U:managers/EntryManager/EntryManagerImpls.ts:252-256`. Generic helper determineType is distinct: existing Blob wins, then listed extension → plain, then bytes → newnote, strings → plain. `U:common/utils.ts:320-331`. | Text writers unconditionally plain, attachments newnote; textual write denied by extension: `livesync_format.py:415-424`; `storage_journal.py:990-1017`; `storage_couchdb.py:907-935`. | Task 4 distinguish device disk ingestion from programmatic supplied string/bytes/Blob. A bytes-or-str API alone does not reproduce every upstream Blob MIME case. Do not claim that determineType alone is the actual final-save decision. |
| .md containing binary bytes | Ordinary device disk path selects **host text decoding**, yields text/plain Blob and plain record if the read succeeds; source does not sniff or reject binary bytes. Invalid encoding byte preservation depends on host read implementation, not this commonlib. A directly supplied binary Blob with .md path is instead newnote. `U:serviceModules/FileAccessBase.ts:201-206`; `U:serviceModules/ServiceFileAccessBase.ts:265-276`; `U:managers/EntryManager/EntryManagerImpls.ts:252-256`. | write_note accepts str only; write_attachment to .md refused by textual-path policy: `storage_journal.py:979-1017`. | Task 4 test both ingress kinds. Do not label arbitrary binary `.md` bytes automatically newnote while claiming device disk parity. Task 8 must observe invalid-UTF8 host decoding. |
| .png containing text bytes | Ordinary device disk path reads binary → non-text Blob → **newnote**, independently base64 pieces. Direct programmatic string/text Blob can be **plain** at the same path; no content sniff. `U:serviceModules/FileAccessBase.ts:201-206`; `U:serviceModules/ServiceFileAccessBase.ts:271-275`; `U:managers/EntryManager/EntryManagerImpls.ts:252-256`. | str write to .png rejected; attachment accepted as newnote: `storage_journal.py:990-1017`. | Task 4 represent the distinction explicitly in API semantics/tests; neither extension-only nor content-only blanket rule covers both upstream entry points. |

### Fix round 1: distinct byte-to-text contracts (P-9)

1. **Disk host read:** `U:serviceModules/FileAccessBase.ts:162-167,201-206`
   selects `adapter.storage.read` / `adapter.vault.read` for plain suffixes.
   `U:serviceModules/ServiceFileAccessBase.ts:265-276` and
   `U:common/utils.ts:97-115` wrap the returned string in a text Blob.
   Commonlib does **not** establish how the native host handles malformed
   UTF-8. Task 8 must observe it. This peer's `plain_ingress_utf8` therefore
   refuses invalid plain byte input before publication. Explicit upload
   `lossy_text=true` uses UTF-8 replacement, reports invalid bytes replaced,
   and returns stored size/hash. This policy is not a host-parity oracle.
2. **Blob-to-string chunk preparation:** v3 calls wheels `readString` at
   `U:string_and_binary/chunks.ts:621,633`; v1/v2 use `Blob.text()` at
   lines 357,445. Real `octagonal-wheels@0.1.57 dist/binary/base64.js:249-298`
   uses a permissive manual decoder at <=128 bytes and TextDecoder above
   that threshold. `A ff B` becomes `AB` in the short branch, while explicit
   ingress replacement gives `A U+FFFD B`. Truncated sequences, overlong
   encodings, lone continuation bytes and UTF-16-surrogate encodings are
   pinned separately in tests. `_wheels_read_string` preserves Task 3c
   behavior; it is never used for disk byte ingress validation.

Execution of the real pinned dependency (including its real imports), with
SHA256 verification against the Task 3c fixture, is retained in
`task-4-fix1-decoder-oracle.mjs/.json` under the Task 4 report directory.
No test claims to establish the native host's malformed-byte semantics.

## г. Path identity, case and NFC

| Question | Exact upstream behavior + citation | Our current implementation + citation | Delta / Task 2–5 action |
|---|---|---|---|
| Normalization order / case flag | PathService splits namespace, applies host normalizePath to body, then NFC only for pathNormalizationVersion=1, rejoins namespace, passes caseInsensitive=`!handleFilenameCaseSensitive`. `U:services/base/PathService.ts:51-63,91-106`; `U:common/pathNormalization.ts:4-21`. path2id_base lowercases only when that argument true. Sensitive mode keeps `A.md` and `a.md` different, both v0/v1. | `_normalize_path` before lowercase, optional NFC before it; sensitive vault refused: `livesync_format.py:88-133,169-175,238-257`. | Task 2 model NFC and case as independent axes (four combinations), not mutually exclusive string modes. Propagate to DB identity and path lookup. Host separator/dot normalization is a separate input contract; path2id_base itself does none. |
| All path2id_base branches | Already `f:` prefixed input returns unchanged **before lowercase**; otherwise lowercase optional, initial `_` escaped to `/_`; no obfuscation returns that value. Obfuscation splits namespace, already-f: body returns prefixed value; else computes keyed document HMAC or legacy SHA256(SHA256(passphrase)+":"+filename), restores namespace + f:. Case choice therefore changes hash input too. `U:string_and_binary/path.ts:74-114`. | Only unencrypted/unobfuscated underscore escaping/lowercase: `livesync_format.py:114-133,158-168`. | Task 2 port sensitive branch for supported unencrypted paths; retain obfuscation refusal (issue #4). Do not blindly lowercase already-obfuscated identifiers. |
| Reverse path and comparisons | id2path_base prefers entry.path (preserves display case); rejects obfuscated bodies without supplied path; removes leading slash escape, otherwise rejoins prefix/body. Ignore matching uses **nocase:true independently** of identity. `U:string_and_binary/path.ts:116-126,183-197`; normal storage/document comparison keys apply optional NFC then optional lowercase, namespace kept for document keys: `U:platform/pathComparison.ts:65-101`. | Display path retained by canonical_path, folder filter unconditionally lowercase: `livesync_format.py:108-111`; `storage_journal.py:623`; CouchDB path2id call unconditionally insensitive: `storage_couchdb.py:944`. | Task 2 inventory all lowercasing in both backends, store and tools; distinguish identity comparison from extension/glob selection instead of making every check case-sensitive. |
| Collision semantics / NFC | Distinct storage names mapping to one ID cause explicit collision error; metadata collision if separate stored IDs map to same identity; requireMatchingIds additionally rejects mismatching old identity and requires rebuild/fetch. With NFC=1, composed/decomposed names collide even in case-sensitive mode; with NFC=0 they remain distinct. `U:common/pathIdentity.ts:5-21,25-51`; `U:common/pathNormalization.ts:19-21`. | NFC aliasing supported, sensitive refused; store indexes revision documents by ID: `livesync_format.py:221-257`; `journal_store.py:274-312,424-457`. | Task 2 preserve two case-variant documents in sensitive mode, reject destructive identity migrations/collisions. Physical filesystem capability remains separate (`U:services/base/VaultService.ts:88-101`); Task 8 must check real host behavior. |

## д. syncMaxSizeInMB — file selection, not journal admission

**Premises:** the exact predicate is `max > 0 && size > 0 && size > max*1024*1024`; ordinary NEW/CHANGED event checks precede reading/saving, so an oversized addition/update creates neither a new local DB revision nor journal records. In contrast, an oversized revision already in DB is not excluded from journal packing by this setting. Journal receive persists records before handing metadata to reflection policy (unless result application is suspended; §ж). The traces below therefore rule out “oversized files cannot exist in the local DB” and “devices reject oversized journal records”.

| Question | Exact upstream behavior + citation | Our current implementation + citation | Delta / Task 2–5 action |
|---|---|---|---|
| Boundary and disabling limit | Strict **greater than** MiB threshold; equality admitted. Positive values (including fractions) bound size; zero/negative disable the predicate. Raising 50→100 admits ≤100 MiB, **does not disable**. `U:services/base/VaultService.ts:73-81`. | Fixed decoded attachment cap=32 MiB and tool download cap=32 MiB: `storage_base.py:15`; `server.py:488-516`; no syncMaxSize check in writers `storage_journal.py:979-1053`. | Task 5 remove arbitrary file caps and apply exact setting predicate where file policy belongs. Zero/unbounded must not produce a derived zero-byte budget. |
| New oversized file added to vault: local DB? user error? | CREATE/CHANGED event loads current stub, returns **true (successful skip)** if oversized, before storeFileToDBFromRevision. File remains in host storage; neither a new local DB revision nor journal metadata/chunks is created for this addition/update. No notice/error in this branch. Other selection path logs “exceeds the current maximum size” at VERBOSE and returns false; chunk-repair skips too. `U:serviceModules/ServiceFileHandlerBase/events.ts:58-69`; `U:serviceModules/ServiceFileHandlerBase/selection.ts:105-113`; `U:serviceModules/ServiceFileHandlerBase/chunks.ts:37-54`. Offline scan also gates storage-only additions (SKIPPED with caller-supplied log level) and existing updates (NOTICE): `U:serviceFeatures/offlineScanner/files.ts:74-85,272-287`; storage-only scan additions call that helper at INFO (`U:serviceFeatures/offlineScanner/scan.ts:173-178`). Thus the user-visible diagnostic depends on event vs scan entry point. | Attachment >32 MiB throws ValueError; text write has no file-size gate: `storage_journal.py:990-1017`; CouchDB same `storage_couchdb.py:907-935`. | Task 5 reproduce **skip synchronization / no publication**, not BinaryNoteError or a claimed rejection of adding a vault file. MCP has no local vault file to retain: explicitly report skipped outcome without pretending persistence. Exact public return shape is an implementation contract to settle in Task 5, not an upstream API. |
| Existing file grows oversized / journal exclusion | Same event early-return leaves prior DB revision intact; no new large revision is generated by ordinary event. Journal packing serializes DB changes without a per-file size selection check. Hence an oversized revision **already in DB** (received or produced by lower-level API) is not excluded merely by current syncMaxSize. `U:serviceModules/ServiceFileHandlerBase/events.ts:63-69`; `U:replication/journal/JournalSyncCore.ts:632-648,712-723`. | Writes publish new revision directly: `storage_journal.py:1027-1053`. | Task 5 no-op before chunks/metadata mutation for an oversized ordinary write; retain old revision, do not delete it. Do not filter journal history by metadata size. |
| Receive oversized journal: local persistence vs vault | Journal receive partitions chunks/docs, saves chunks new_edits=true then missing metadata revisions new_edits=false **without this size predicate**, then calls processReplication only when suspendParseReplicationResult is false (§ж). `U:replication/journal/JournalSyncCore.ts:778-823`. Plugin result processor checks metadata.size, emits NOTICE “skipped due to file size exceeding the limit”, returns before applyToDatabase. `P:serviceFeatures/replication/ReplicateResultProcessor.ts:1050-1070`. Commonlib processReplicatedDocument also checks entry.size (VERBOSE policy skip) and existing target storage file size before dbToStorage. `U:serviceModules/ServiceFileHandlerBase/events.ts:211-247`. | Replay stores metadata/chunks regardless of syncMaxSize: `journal_store.py:234-312`; reads reconstruct directly: `journal_store.py:460-534`; independent parser/object limits apply (`journal_codec.py:54-57`, `object_storage.py:45`). | Task 5 **keep replay persistence**; separate user-visible read/reflect policy from journal admission. Do not reject a whole journal because a metadata.size exceeds this peer's local limit. Device NOTICE belongs to reflection, not an ingest failure. |
| Raising setting / rescan; direct APIs | Saving changed settings also triggers retry: syncMaxSizeInMB is included in the normal-file reflection-filter signature; EVENT_SETTING_SAVED compares signatures and calls reprocessStoredDocuments when changed. That method enumerates stored normal documents and requeues them. `P:serviceFeatures/replication/automaticTriggers.ts:27-37,55-66`; `P:serviceFeatures/replication/ReplicateResultProcessor.ts:718-730`; wiring `P:serviceFeatures/replication/index.ts:130-136,172`. A fresh eligible event/scan can save local content or reflect received metadata. No universal size validation exists in low-level prepareStoreEntry/EntryManager save, which handle typed Blob directly. `U:serviceModules/ServiceDatabaseFileAccessBase.ts:195-232`; `U:managers/EntryManager/EntryManagerImpls.ts:238-282`; scan file-size checks `U:serviceFeatures/offlineScanner/files.ts:268-306`. | Writers bypass vault selection and do not consume size setting: `storage_journal.py:979-1053`. | Task 5 choose ordinary-device semantics for MCP write operations; retain received DB data. Task 8 verifies materialization after saving a raised/disabled limit; the automatic stored-document requeue trigger is established by source, not an open question. Requeue does not guarantee materialization when other policies, suspension or conflicts still block it. |
| Can replay/download budgets be derived solely from max file size? | No: receive accepts records regardless of local file policy; journals contain multiple documents and may be made by peers with other settings. V3 caps **pieces** at M (+3 bytes for valid UTF-8 text; malformed text Blobs require separate handling), not journals or total replay. `U:string_and_binary/chunks.ts:527-542,617-633`; `U:replication/journal/JournalSyncCore.ts:638-648,778-823`. | 64 MiB line, 1 GiB inflate, 100000 records; 512 MiB object, 4 GiB/300s refresh: `journal_codec.py:54-57`; `object_storage.py:45`; `storage_journal.py:137-140`. | Task 5 must revise plan's proposed download-limit=oversized path and replay-from-local-syncMax assumption: neither is a journal protocol rule. Bound generated chunk lines by actual encoding/escaping and algorithm, account for old/foreign peers and metadata lines; any operational budgets must be explicit overrides or documented non-parity behavior. |

**Raw admission versus converted Blob (Task 5 fix round 1, P-13):**
Device ordinary admission checks host `stat.size` (raw bytes); EntryManager
converts content to a typed Blob and stores `data.size` as metadata.size
(`C:managers/EntryManager/EntryManagerImpls.ts:253-282`). These quantities can
differ: one invalid UTF-8 byte becomes a three-byte replacement character.
The peer retains the raw-byte admission gate and additionally gates converted
Blob bytes before publication. If either exceeds the vault limit, report SKIP,
explicitly state that new content was NOT saved, and preserve the old revision.
For oversized rename destinations, P-13 supersedes device DELETE(source):
REFUSE with zero mutations because the peer has no retained host file.
JSON depth overrides set admission thresholds, not the Python decoder's
recursion capacity (normally below roughly 1000 nesting levels). Capacity
exhaustion must report a depth-budget error with observed nesting, configured
limit and variable, as documented in README; it is not invalid JSON syntax.

**Send/receive asymmetry:** ordinary oversized NEW/CHANGED input is skipped before storeFileToDBFromRevision (`U:serviceModules/ServiceFileHandlerBase/events.ts:68-69`): no new local DB revision and no generated journal records. Once a revision is already in DB, journal packing has no syncMaxSizeInMB exclusion (`U:replication/journal/JournalSyncCore.ts:638-648`); receive persists it, while vault reflection can remain blocked. “Local-DB-yes/journal-no” is not the ordinary send behavior. Existing journal deduplication/checkpoints still apply.

**Rejected paths:** “refuse to add the physical file” is contradicted by event's early successful return; “send metadata but omit oversized chunks” is contradicted by checking before EntryManager; “receive rejects oversized doc” is contradicted by persistence before reflection policy; “raise to a positive value means unlimited” is contradicted by the predicate. These conclusions apply to ordinary selected files, not all internal/low-level API paths.

## е. Eden

| Question | Exact upstream behavior + citation | Our current implementation + citation | Delta / Task 2–5 action |
|---|---|---|---|
| useEden=false; count/length/age controls | In this audited writer, metadata **always** gets eden:{} and leaves are independently written, with no branch consulting useEden/maxChunksInEden/maxTotalLengthInEden/maxAgeInEden. Settings remain in tweak compatibility template (`U:common/models/tweak.definition.ts:33,38-40`) but EntryManager write path is `U:managers/EntryManager/EntryManagerImpls.ts:126-149,244-282`. Thus false has no extra journal-record effect; **true also does not activate incubation in this writer**. This is current code behavior, not a claim about all historical releases. | Writes eden:{}, reads referenced inline legacy chunks: `livesync_format.py:391-399,446-471`; misleading docstring calls Eden E2EE-only at `livesync_format.py:384-389`. | No writer change required for current Eden settings. Task 7 correct E2EE-only explanation. Do not implement imagined Eden thresholds from labels. |
| Read historical Eden / journal shape | Reader materializes meta.eden entries as leaf docs to resolve children: `U:managers/EntryManager/EntryManagerImpls.ts:612-636`; journal metadata serializer preserves JSON fields as given: `U:replication/journal/journalRecords.ts:70-76`. | extract_inline_chunks and JournalStore resolve Eden: `livesync_format.py:446-471`; `journal_store.py:536-574`. | Preserve read support and inline metadata on applicable operations; do not discard a received nonempty Eden just because current setting is false. |

## ж. Other tweak_values and production-evidence boundary

**Evidence gap:** no raw production milestone is checked into `docs/test-artifacts/2026-11-20-live-s3/`; client-log contains only replication tail and no settings (`client-log-replicate.md:1-16`). The only checked-in milestone builder is **synthetic**, with only PREFERRED.pathNormalizationVersion (`tests/test_storage_journal.py:57-71`). `tests/fixtures/live_client_journal_sample.bin` is a journal, not the `_`-prefixed milestone control object. Thus the supplied observation “production useEden=false” is context, not independently confirmed here; a production-key/value enumeration cannot honestly be manufactured. The following is a complete **upstream tweak template inventory**, not claimed production values, plus explicitly identified local settings outside that template. “Irrelevant to record shape” does not mean irrelevant to peer compatibility: see the compatibility section below. Obtain a sanitized real milestone in Task 8 and reconcile every key against it.

| Key(s) / question | Upstream effect and citation | Our status + citation | Parity classification / action |
|---|---|---|---|
| minimumChunkSize, customChunkSize, chunkSplitterVersion, hashAlg | Changes pieces / IDs, not framing: §а/б; `U:common/models/tweak.definition.ts:25,34,36,61-65`. | Whole SHA chunk `livesync_format.py:354-371`; writers above. | **Affects parity**; Task 3. |
| handleFilenameCaseSensitive, pathNormalizationVersion | Changes document identity; §г; `U:common/models/tweak.definition.ts:30,48-51`. | NFC only, sensitive refusal `livesync_format.py:221-257`. | **Affects parity**; Task 2. |
| syncMaxSizeInMB | File selection, not record schema; recommended template value 50: `U:common/models/tweak.definition.ts:130`; §д. | Not consumed, fixed caps `storage_journal.py:1006-1017`. | **Affects parity**; Task 5. |
| longLineThreshold | Template key, unused by v3: §а; `U:common/models/tweak.definition.ts:26`. | Not consumed `storage_journal.py:979-1053`. | **Irrelevant to v3 output**; do not invent behavior. |
| useEden, maxChunksInEden, maxTotalLengthInEden, maxAgeInEden | Retained template; no active writer branch: §е. | Empty Eden write `livesync_format.py:391-399`. | **Irrelevant to current writer output**; retain historical reading. |
| enableChunkSplitterV2, useSegmenter | Legacy migration inputs **only when chunkSplitterVersion is empty**: selects v2 or v2-segmenter; v3 ignores them. `U:services/base/SettingService.ts:141-158`; `U:common/models/tweak.definition.ts:37,53`. | No version/migration selection `storage_journal.py:979-1053`. | **Affects parity for legacy modes**, irrelevant with explicit v3. Task 3 cannot call a v3-only port “any settings” parity. |
| encrypt, usePathObfuscation, idDerivationVersion | Changes transport encryption / identities; §б/г; `U:common/models/tweak.definition.ts:27-29`; `U:replication/journal/JournalSyncCore.ts:513-526`. | E2EE/obfuscation refusal `livesync_format.py:158-168`. | **Affects parity, explicitly deferred** to #3/#4; retain supported-scope refusal. |
| E2EEAlgorithm, useDynamicIterationCount | Select journal transport encryption variant/work when encrypt active: `U:replication/journal/JournalSyncCore.ts:513-526`; template `U:common/models/tweak.definition.ts:35,55-59`. | Encryption unsupported as above. | **Irrelevant when encrypt=false**; encrypted parity deferred #3. |
| encryptInternalMetadata | Effective tweak forces false outside CouchDB: `U:pouchdb/LiveSyncDBFunctions.ts:19-23`; encrypted metadata is separate feature. | Service/internal docs excluded `livesync_format.py:431-443`. | **Irrelevant to ordinary unencrypted S3 journal records**; do not infer hidden sync support. |
| enableCompression | CouchDB document transform can compress data, but S3 packing unconditionally deflates serialized journal records, independent of this flag: `U:pouchdb/compress.ts:49-71,85-98`; `U:replication/journal/JournalSyncCore.ts:638-648`. | Raw-deflate journal codec `journal_codec.py:161-186`. | **Irrelevant to S3 logical record shape**; do not confuse CouchDB compressed data with base64 newnote or transport deflate. |
| usePluginSyncV2 | Plugin/hidden sync feature selection, not ordinary file chunk serialization; selection `P:features/ConfigSync/CmdConfigSync.ts:394-396`; `P:features/HiddenFileCommon/optionalFileSyncSelection.ts:355-365`; template `U:common/models/tweak.definition.ts:41-46,132`; ordinary record writer `U:managers/EntryManager/EntryManagerImpls.ts:274-282`. | Internal namespaces excluded `livesync_format.py:431-443`. | **Irrelevant to ordinary file record schema**; may change which extra metadata is synchronized; outside ordinary-file parity scope. |
| useIgnoreFiles, ignoreFiles | Selection policy, not serialization; template `U:common/models/tweak.definition.ts:116-130`; acceptance matcher `U:string_and_binary/path.ts:183-231`; actual setting gate and matcher `U:serviceFeatures/targetFilter.ts:132-146`. | Hidden/path policies `livesync_format.py:260-296`; no equivalent vault ignore configuration. | **Affects file selection parity**, not byte framing. Not implemented by Tasks 2–5 as written; record residual gap rather than silently asserting universal parity. |
| useCustomRequestHandler, batch_size, batches_limit, useTimeouts | Transport/batching knobs in recommended template, not logical piece/doc schema: `U:common/models/tweak.definition.ts:117-123`; journal serializing code `U:replication/journal/journalRecords.ts:70-76`. | Independent object adapter/budgets `object_storage.py:112`; `storage_journal.py:137-140`. | **Irrelevant to logical journal-record parity**; packing/timing may differ. |
| readChunksOnline, hashCacheMaxCount, hashCacheMaxAmount, concurrencyOfReadChunksOnline, minimumIntervalOfReadChunksOnline | Retrieval/cache knobs in template `U:common/models/tweak.definition.ts:124-128`; cached legacy ID can be reused `U:managers/EntryManager/EntryManagerImpls.ts:381-383`. | Chunk reuse and retrieval `storage_couchdb.py:659-711,790-840`; `journal_store.py:536-574`. | **Irrelevant to fresh deterministic record schema**, cache history can affect ID reuse (§б); no invented splitter effect. |
| tweakModified / autoAcceptCompatibleTweak | tweakModified is in the template; autoAcceptCompatibleTweak is a local control outside it. Compatible-lossy automatic alignment chooses the newer positive timestamp; ties/both absent choose REMOTE, one absent loses to the positive timestamp. Undefined autoAcceptCompatibleTweak is persisted as true. `U:common/models/tweak.definition.ts:137-150`; `P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:120-151`. | Only recognized identity settings read `livesync_format.py:145-152,179-219`; no equivalent alignment policy. | **Affects peer behavior**, not record shape; compatibility section defines startup refusal, without silently adopting settings. |
| suspendParseReplicationResult (outside tweak template) | Journal chunks/metadata persist before the flag gates processReplication; settings-save handling suspends/resumes application. `U:replication/journal/JournalSyncCore.ts:778-823`; `P:serviceFeatures/replication/automaticTriggers.ts:59-63`. | Peer replay persists data and reads reconstruct directly; no filesystem reflection stage or suspension gate: `journal_store.py:234-312,460-534`; `storage_journal.py:653-678`. | [DECISION] **Accepted deviation for the current MCP peer**: database-backed reads remain available regardless of another device's local reflection suspension. This flag is not advertised in tweak_values and does not block upstream persistence. No Task 2–5 implementation requirement; revisit if the peer acquires a local vault reflection stage. |
| syncOnlyRegEx / syncIgnoreRegEx (outside tweak template) | Ordinary writes require at least one allow-list regex match when compiled allow patterns exist; any ignore-list match rejects. Save checks isTargetFile before chunks; vault selection delegates to the local DB target filter. `U:managers/EntryManager/EntryManagerImpls.ts:247-249,343-369`; `U:serviceFeatures/targetFilter.ts:68-75`. | No corresponding regex selection in writers: `storage_journal.py:979-1053`; `storage_couchdb.py:907-960`. | **Residual selection parity gap, added to Task 4 scope**: peer WRITES must apply the same ordinary-file target filter and refuse creating notes excluded by the configured vault regex rules before mutating chunks/metadata. These are local settings, not PREFERRED tweak keys; do not infer their values from the milestone. Preserve upstream regex parsing/matching semantics, including case flags from handleFilenameCaseSensitive and the custom list parser (`U:common/utils.ts:545-556`), rather than assume Python regex equivalence. |

## Tweak compatibility and peer startup requirements

**Premises:** devices compare effective local tweak_values with milestone PREFERRED, falling back to current values when PREFERRED is absent. idDerivationVersion differences can return ID_KEY_MISMATCH; pathNormalizationVersion mismatch returns MISMATCHED before the disableCheckingConfigMismatch gate. Other assessed mismatches return MISMATCHED when checking is enabled (`!disableCheckingConfigMismatch`). `U:pouchdb/LiveSyncDBFunctions.ts:79-118`. Journal ensureBucketIsCompatible invokes this check, and connectivity returns false on MISMATCHED while retaining preferred values and assessment. `U:replication/journal/LiveSyncJournalReplicator.ts:209-249,542-553`.

**Execution trace:** assessTweakCompatibility iterates TweakCompatibilityRules, resolves missing values using declared known defaults, distinguishes equal/different/unadvertised, and does not equate a representation-only difference with a semantic mismatch. A missing value without a known default is unadvertised. idDerivationVersion is treated as equal when neither side uses keyed document IDs. Alignment is mismatched when any assessed entry is different. `U:common/models/tweak.compatibility.ts:57-65,148-191`. Recommendation-only template keys are not all compatibility-comparison keys: the rule inventory is `U:common/models/tweak.definition.ts:24-67`.

When every difference is compatible-lossy (customChunkSize, hashAlg, chunkSplitterVersion) and no entry is unadvertised, plugin auto-accept may align settings if autoAcceptCompatibleTweak is true (undefined is saved as true). The greater positive tweakModified timestamp wins. If both are absent/nonpositive, or positive timestamps tie, REMOTE wins; if only one side has a positive timestamp, that side wins. `U:common/models/tweak.definition.ts:34-36,61-65`; `U:common/models/tweak.compatibility.ts:177-191`; `P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:120-151`.

[DECISION] **Peer-parity requirement, added to Task 2 startup settings validation:** before serving vault operations or publishing journal records, compare the peer's effective supported settings against PREFERRED with the same assessment meaning. Detect semantic mismatches and refuse startup with the differing key names; do not silently proceed or silently adopt settings. Preserve absent/known-default/unadvertised distinctions, the pre-gate path-normalization rejection, and the separate ID-configuration refusal. Report key names without secret values. A fail-closed peer does not need the plugin's interactive/automatic resolution UI, but must explain that alignment is required. This is a requirement for subsequent implementation, not a claim that the current peer implements the check.

Task 5b implements the assessment and startup checks in both backends. `backend_info`
reports only classification and differing keys. CouchDB obtains the same PREFERRED
snapshot from `_local/obsydian_livesync_milestone`: `U:pouchdb/LiveSyncDBFunctions.ts:177-203`,
`U:replication/couchdb/LiveSyncReplicator.ts:2304-2305,2358-2372`,
`U:common/models/db.const.ts:4`. Device snapshots are checked independently against
that baseline and the concrete effective writer; this extra peer startup policy
does not mutate their advertised values. Unadvertised relations remain intact
within each pair. The CouchDB peer advertises enableCompression=false because it
has no compression adapter, unlike the upstream CouchDB writer
(`U:replication/couchdb/LiveSyncReplicator.ts:332,2097`). Journal framing does not
use that adapter.
The peer advertises `encryptInternalMetadata=false` for its plaintext writer,
matching `getEffectiveTweakValues` (`U:pouchdb/LiveSyncDBFunctions.ts:19-24`).

[DECISION] Automatic lossy alignment applies the upstream timestamp winner to the
local writer, with a log. The device has additional remote side effects:
CURRENT auto-accept updates remote PREFERRED
(`P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:229-231,333-339`).
REMOTE auto-accept saves local settings, reinitializes the chunk-generation
managers when no rebuild is required, and ALSO updates remote PREFERRED
(`P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:341-350`). After a
successful compatibility check, `ensureRemoteIsCompatible` publishes the device's
current tweak snapshot when updating the milestone (including its periodic
last-connected update); it initializes PREFERRED if absent
(`U:pouchdb/LiveSyncDBFunctions.ts:153-168`, publication callback at :170).

By explicit policy this peer remains an invisible writer: it publishes note
changes but neither its device snapshot nor PREFERRED. The Task 5b execution
brief requires local alignment without those device milestone writes. Remote
alignment remains an explicit device operation. A CURRENT winner therefore does
not prove the other devices have aligned.

[DECISION] Public-API difference: Python runs the Task 3c one-pass splitter
migration on both assessment inputs before comparison. The pinned assessment
itself does not migrate;
its raw missing `chunkSplitterVersion` uses the declared v3 default, whereas the
migration maps a missing version to empty (the existing peer legacy writer path).
Differential fixtures retain both raw and normalized oracle assessments so this
boundary is explicit. `U:services/base/SettingService.ts:141-158`;
`U:common/models/tweak.compatibility.ts:57-65,148-191`.

**Rejected paths:** serializing identical logical records does not establish compatibility; unused writer knobs can still be compared and block a peer. tweakModified selects an alignment side, not journal ordering. Regex filters and reflection suspension are local controls outside tweak_values and must not be fabricated from PREFERRED.

## Audited versions, usable vectors, and live questions

Audited commonlib: **484c164450d61deb2e301582b60cb9b5f9fa977f**, fresh clone default HEAD on 2026-11-23; plugin **1.0.52 / 5070b67beae3b60e3a79132386a08da3f1ef213a**, package dependency commonlib 0.7.0 (`P:../package.json:223`). Historical production acceptance remains **1.0.50**, per `docs/upstream-pin.md:1-9` and `docs/test-artifacts/2026-11-20-live-s3/client-log-replicate.md:16`. Source audit does not prove installed-client behavior.

Usable upstream vector sources (all U paths are relative to src/ at the commit above):

| Source | Available vectors / use |
|---|---|
| `string_and_binary/chunks.rabin-karp.unit.spec.ts:5-127` | Exact zero/marker binary boundaries, insert/delete suffix reuse, 30 KiB floor / 4 MiB ceiling, minimum override, text marker [256,256,256], UTF-8 max+3. Task 3. |
| `string_and_binary/chunks.rabin-karp.unit.spec.ts:130-220` | Empty input, 1 MiB read-block crossings, retained rolling window [1048641,435], UTF-8 crossing. Task 3. |
| `worker/bg.worker.splitting.rabin-karp.unit.spec.ts`; `ContentSplitter/ContentSplitterDirectWorker.unit.spec.ts` | Worker/direct equivalence and splitter routing; executable fixture recipes, not a substitute for byte-boundary vectors. Task 3. |
| `managers/HashManager/HashManager.unit.spec.ts:17-31,116-141,249-256` | Literal helloWorld legacy/base36 hashes for every algorithm, encrypted/unkeyed-v1 branches. Plain xxhash64 = 37me21jj44pcc. Task 3. |
| `common/idDerivation.unit.spec.ts:31-55` | Fixed keyed chunk/document HMAC vectors and malformed settings cases. **E2EE-context vectors**, not the plaintext algorithm. |
| `string_and_binary/path.unit.spec.ts:43-78`; `services/base/PathService.unit.spec.ts:55-125`; `common/pathNormalization.unit.spec.ts` | Case variants, underscores/obfuscated legacy IDs, NFC and namespaces, absent/0/unsupported versions. Task 2. |
| `managers/EntryManager/EntryManager.pathIdentity.unit.spec.ts`; `managers/EntryManager/EntryManager.unicodePaths.unit.spec.ts`; `replication/journal/objectstore/unicodePaths.integration.spec.ts` | Identity collision / Unicode integration cases. Task 2 and matrix. |
| `managers/EntryManager/EntryManagerImpls.unit.spec.ts:1658-1691`; `serviceModules/FileAccessBase.unit.spec.ts:687-707,746-769` | Save matrix uses typed Blobs and sizes/extensions; host text/binary read-path fixtures. Task 4. No fixed invalid-UTF8 native Obsidian decoding vector found. |
| `replication/journal/journalRecords.unit.spec.ts`; `serviceModules/ServiceFileHandlerBase/events.unit.spec.ts`; `serviceFeatures/offlineScanner.unit.spec.ts` | Framing/corruption and size-policy fixtures. Current-file size skip tests are policy mocks, not proof of the real host's notice/rescan timing. Task 5/6. |

Task 8 live experiments MUST answer:

1. Capture **sanitized actual milestone**, installed plugin version and settings before/after, including complete tweak_values keys. Confirm observed splitter/custom/minimum/Eden/hash/identity values rather than substituting upstream defaults; determine per-device vs PREFERRED differences.
2. At positive limit L, add fresh files exactly L MiB and just above L; then grow an existing synced file above L. Verify physical file retained, local DB revision unchanged for skipped addition/update, and absence of newly generated journal chunks/metadata. Record logs/notices. Test a >32 MiB file below raised limit with exact byte/chunk/ID comparisons in both directions.
3. Produce oversized journal metadata on a permissive sender; receive on a peer with smaller positive limit. Verify DB records persist, vault reflection skips with NOTICE; raise the limit and save settings, then verify actual materialization after the source-established automatic requeue. Repeat with zero (unlimited). Record remaining policy/conflict/suspension blockers if materialization fails; the retry trigger itself is not an open question.
4. On the real Obsidian host, read invalid UTF-8 bytes under `.md` and valid text bytes under `.png` (also uppercase suffix). Record host decoding/errors, saved Blob-derived document type, byte size and chunk data; commonlib supplies no guarantee of invalid-byte preservation by the host text reader.
5. Case-sensitive + NFC vault on actual filesystem: two case variants vs two canonically equivalent Unicode spellings. Check collision diagnostics and physical host aliases independently of journal identity. Capture native client journal as the golden fixture, including the ≥4 MiB text-threshold transition and binary multi-piece records.

[DEFER] Universal “any settings” parity additionally requires legacy splitter/hash modes and selection/ignore policies listed above, or a narrower supported-settings claim. Task 2 additionally owns startup compatibility refusal; Task 4 additionally owns regex filtering of peer writes. These requirements are not completed by this source extraction. Neither source evidence nor Task 8 outcomes should be replaced by new arbitrary limits.


## P-14 correction: journal epoch, wipe and identity recovery (Task 5 fix round 2)

**Superseded:** the earlier Plan B requirement “epoch change → full local state
reset (SQLite tables and chunks)” is historical, not the current contract.
See the explicitly superseded reset/identity instructions in
[the original implementation plan](../plans/2026-11-19-s3-journal-peer.md).
P-15 scopes P-13 preservation to peer-initiated mutations refused or skipped
before application; an uncertain publication cannot promise unchanged content.
This peer has no separate host filesystem copy to recover from.

Here `C:` is the commonlib source root (the same as `U:` above).
At commonlib `484c164450d61deb2e301582b60cb9b5f9fa977f`,
`C:replication/journal/JournalSyncCore.ts:291–341` updates checkpoint records and
resets `knownIDs`, `sentIDs`, `receivedFiles`, `sentFiles`, without touching the
local database. `:343–400` derives the epoch, keeps caches if the epoch is equal,
and on epoch change keeps caches if there is no send history or the last-sent
file still exists. Only a missing last-sent file (or failed upstream listing
probe) confirms a wipe and clears those caches. Thus the code refines the P-14
brief: **an epoch change alone does not clear even dedupe caches**. Real pinned
methods, not a Python/JavaScript reimplementation, are executed by the Task 5
fix-round-2 checkpoint oracle, `scripts/probe_journal_checkpoints.mjs`.
Run `node scripts/probe_journal_checkpoints.mjs /path/to/pinned/commonlib`;
it verifies the source against the pin and emits JSON with all six scenarios.

The peer retains every document revision and chunk on recovery; it clears only
replication tracking (processed journals, outbound records and last-sent) and
replays from the beginning after a confirmed wipe. Insert-if-missing revision keys and content-addressed chunks
make this idempotent. Notes absent from the new world intentionally survive
locally and are not automatically republished; a fresh peer cannot see them.
The peer additionally checks its newest confirmed outbound journal (legacy
state falls back to last-sent) even at the same epoch, and
propagates probe IO failures rather than interpreting them as a confirmed wipe.
This is a conservative extension of upstream, preserving prior peer wipe
protection and the P-13 refusal invariant.

The pre-append probe is read-only: changed epoch or a missing newest confirmed
outbound journal raises `ConcurrentModification` (“remote was rebuilt or journals
are missing; refresh and retry”), without changing store state or publishing anything.
Startup and refresh reconcile caches without deleting local content, including
when the first replay download fails. Install staged chunks before committing
metadata; an install failure leaves previous revisions readable. A failed apply
may leave unreferenced content-addressed chunk files, reused on retry, but must
not commit metadata referencing missing chunks or mark the journal processed.

A path-identity change must never mix IDs or discard data: close the old store,
move the entire state directory to the sibling
`<state_dir>.quarantine/<UTC timestamp and unique suffix>/state`, then create a
fresh store. Log a WARNING with the archive path and restoration instructions.
The old store remains openable under its original identity. Restoration requires
stopping all users, moving the new state aside, restoring the archive to the old
path, and configuring the original identity. No automatic deletion or eviction.


## P-15/P-16 outcome contract (Task 5 fix round 4)

Preservation concerns peer-initiated mutations, not incoming replay. Replay
applies remote truth incrementally; a later download failure does not roll back
journals already applied. The three closed round-2 loss paths remain unchanged.

A mutation reports applied only after confirmed durable publication (and local
metadata commit for journal). Explicit pre-application refusals remain ordinary
errors/skips with no requested mutation. Proven pre-send failures and explicit
4xx refusals remain ordinary errors. A lost metadata response after send,
unconfirmed metadata acknowledgement, or failure after remote journal
publication but before local commit raises distinct `OutcomeUnknown`.
Every mutating tool returns operation-specific text beginning `outcome unknown`
and stating that the change MAY have been applied.

Uncertainty starts only when a request publishing the note's **metadata**
has been sent and its outcome is unknown (P-16). Losing a chunk PUT response
before metadata publication is an ordinary error: the note is unchanged; a
harmless orphan chunk may exist. Copy and move reuse chunks. If move publishes
the destination but loses the source-retirement metadata response, it reports
`destination published; source removal unknown — re-read both paths`.
An S3 journal publishes metadata and chunks together in one request. Force a
journal refresh before re-reading paths after an unknown outcome.

Every mutating tool names its operation and retry class:

- **NON-IDEMPOTENT:** append, prepend, search-replace, after-heading insertion,
  canvas node add and canvas edge add. A blind retry can duplicate the change:
  **re-read the note and apply the remaining change manually rather than
  retrying blindly**.
- **IDEMPOTENT FOR THE SAME INPUT:** overwrite, delete, move, copy, template
  create, note create, attachment upload, properties set/delete, tags add/remove
  and canvas node/edge remove. **Re-read to see whether it applied; retrying the
  same request is safe**. Create repeats may report “already exists”; tags add
  deduplicates. This describes the same request, including paths, values and IDs.

The journal peer commits a pending outbound key and payload digest locally
before sending, then confirms it after exact remote read-back. Replay/refresh
recognizes its own matching publications independently of `last_sent_key`,
applies and marks them processed, and repairs last-sent to the newest confirmed
outbound journal. Reconciliation is idempotent. An unpublished pending intent
is removed when its key is absent; a colliding key with different bytes is not
claimed. Removing an obsolete old key after reconciliation causes no refusal;
a missing newest confirmed journal still triggers wipe detection. Resets retain
local note revisions and chunks.

After remote read-back and local metadata commit, processed/last-sent tracking
failure retries at most three times, then logs WARNING and returns success.
The next replay repairs deferred tracking without requiring another mutation.
This reporting contract does not introduce an emulated upstream parity oracle;
checkpoint compatibility checks execute the real pinned commonlib methods.


## P-18/P-19: typed diagnostics (Task 5b fix round 2)

P-19 supersedes the P-18 blacklist approach. Every public error is constructed
from a closed reason code and typed fields: caller note paths (control escaping
only), remote metadata paths (NFC plus escaping), inventoried setting keys,
validated numeric fields, closed enum names and exception **classes**. A free
`detail` string is not a public channel. Unknown failures, including generic
exceptions at the tool boundary, expose only the outcome/context and cause
class. Existing mutation classification and the 19 retry guidance variants
remain unchanged. Part B comparison, election and published fixtures are
unchanged; only their public field projection is constrained.

Journal filenames are shown only for `<digits>-docs.jsonl.gz` or
`<digits>-<32 hex>-docs.jsonl.gz`; other names become
`<unexpected journal name>` plus length. Default logs omit response fields,
exception text/tracebacks, setting values and selection patterns in ordinary
storage/selection logs. Lifespan exceptions propagate to the hosting framework;
it controls startup traceback logging. Outcome-rendering failures log full
exception details but return an outcome-unknown message to the tool caller. The only
free-text sink is explicit `LIVESYNC_UNSAFE_DEBUG=1` at DEBUG, documented as
unsafe for production; its last-line redactor caps inspection at 2048 characters
and withholds nonempty inputs wholesale. There is no regex secret detector on the
tool path. This gives O(min(n, 2048)) time and space for that sink.

The pinned commonlib `replication/journal/JournalSyncCore.ts:344` forms the
internal epoch from protocol version and salt; `:467–468,523–524` passes that
salt with the passphrase to binary HKDF decryption/encryption. The salt is
stored in remote sync parameters (`replication/SyncParamsHandler.ts:124–149`),
so it is protocol metadata rather than a demonstrated authentication secret.
Nevertheless its public diagnostic representation is a 16-hex SHA-256
fingerprint of the epoch; internal epoch/replay comparisons retain the original.
No substitute upstream oracle was introduced (P-7).


### P-21: typed diagnostic validation (Task 5b fix round 3)

Transport diagnostics retain HTTP status, operation and sanitized cause class,
including S3 failures wrapped by journal publication/readback. Operations are
closed to GET/PUT/POST/DELETE/HEAD/LIST. Causes match the ASCII identifier charset
`[A-Za-z_][A-Za-z0-9_]{0,63}` or become `<unexpected cause>`. Other identifier-like
fields retain their closed reason/key/budget/algorithm inventories. Diagnostic
numbers use per-field types (P-22): counts, sizes/byte limits, depths and HTTP
status codes are `int`; measured limits and durations accept `int` or finite
`float`, all in `0 <= x < 2**63`. Process exit statuses are `int` in `[-256, 256]`.
Booleans are excluded. Numeric fields are validated at construction and again
at rendering; malformed numbers become `<invalid>`. Measured floats round to
12 significant digits and render as plain decimals without trailing zeros;
integers remain exact. Plain notation also applies below `1e-6` and at/above
`1e16`; out-of-range values remain `<invalid>`.
Environment replay counts/sizes/depths are positive integers below `2**63`.
Refresh duration settings accept positive finite integers or floats below
`2**63`, restoring fractional refresh seconds and refresh intervals.

Free-text paths escape C0/C1, CR/LF/tab, U+2028/U+2029, bidi embeddings/overrides
U+202A–U+202E and isolates U+2066–U+2069. Zero-width U+200B/U+200C/U+200D and BOM
U+FEFF pass unchanged because they can belong to note identities and do not
reorder text. Structured JSON paths remain exact and addressable. Renderer
exceptions cannot escape the tool wrapper: the operation's result is unknown,
and callers must re-read before retrying. The last-line redactor also accepts
non-string inputs defensively, withholding them without invoking object coercion.


### 2026-11-24 — G7 surrogate publication

Status: implemented with upstream-generated wire vectors, publication/replay
coverage for both backends, and typed tool reporting.

[GOTCHA] Upstream journal transport loses unpaired UTF-16 units in compact
leaves. Premises: commonlib `484c164450d61deb2e301582b60cb9b5f9fa977f`,
`src/string_and_binary/chunks.ts:243-252` cuts non-plain V1 by UTF-16 units;
`src/replication/journal/journalRecords.ts:69-77` uses TextEncoder directly for
compact leaves, but JSON.stringify for metadata; `:79-108` decodes with fatal
UTF-8. Execution: the `a*123 + U+1F642 + z` witness, `.MD`, V1, SHA-1,
customChunkSize=-0.9987841796875, minimumChunkSize=5, splits the emoji into
U+D83D and U+DE42. Each compact frame replaces its isolated unit with U+FFFD;
every replaying device therefore reconstructs two U+FFFD characters. This is
an upstream transport defect, reproduced rather than repaired by the peer.
Rejected path: shifting cuts would change device pieces and IDs.

[DECISION] G7 preserves cuts/IDs and publishes identical pre-deflate bytes.
Metadata and CouchDB JSON preserve surrogate units with lowercase escapes;
PouchDB adapter-http 9.0.0 `lib/index.es.js:765-773` sends JSON.stringify(doc).
Compact journal publications report a typed integer `transport_replacements`
only when nonzero, with a fixed explanatory tool sentence. A fresh peer sees
exactly the upstream transport result. No environment variable or device
behavior difference is introduced. Oracle provenance, pinned source hashes
and verified dependency integrity are committed with the generated fixture.
S1 remains a separate stopped task; its preserved draft is untouched.

Source correction to the task premise: V2 `chunkStringGenerator` does protect
surrogate pairs (`chunks.ts:307-310`, called at `:362` for non-plain text).
The committed oracle also executes V2/V2-segmenter on this witness to pin that
distinction. The demonstrated split-pair transport loss is V1 non-plain.


### 2026-11-24 — G1 ignore-file derivation

[DECISION] Resolve useIgnoreFiles and ignoreFiles independently from PREFERRED
or agreeing device snapshots unless explicitly overridden. Empty environment
variables mean derive. Regex settings remain env-only (absent from the upstream
template). Active vault ignore policy requires Node.js 22+; explicit false can
produce peer-only ghost notes that devices do not reflect. Reads bypass all
filters. Journal refresh checks milestone content changes and revalidates the
effective policy before writes; rules fingerprints separate cached verdicts.

Source: commonlib tweak.definition.ts:116-149, setting.const.defaults.ts:140-141,
serviceFeatures/targetFilter.ts:95,134-145,205 and
serviceModules/ServiceFileHandlerBase/events.ts:217-219 at the audited pin.
The two service files are in commonlib, rather than the plugin checkout named
in the task premise. Existing journal refresh did not reread milestone settings;
G1 adds that check for ignore-file policy.


2026-11-24 G1 follow-up: [GOTCHA] The existing unreadable-ignore handler named
StorageError without importing it, so storage/decoding failures raised NameError.
Import the exception and retain the upstream absent-file result. The tweak
oracle now executes the actual getIgnoreFile catch path (commonlib
serviceFeatures/targetFilter.ts:102-127); regression coverage exercises both
backends with storage and decoding failures. Keep this separate fix as a
follow-up commit without rewriting the derivation commit.


### 2026-11-25 — G2 CouchDB compression

[DECISION] Apply the remote document transform at direct/batched read and PUT
boundaries for all string data fields, including leaves and newnotes. Always
decode marked reads, including with mismatch bypass enabled. Write according to
PREFERRED, otherwise the first readable settings map, default false; reject
non-booleans. Mixed compression settings do not block startup or appear as a
compression mismatch. Report the effective boolean in vault_stats. Journal
behavior is unchanged. Sources: commonlib src/pouchdb/compress.ts:9-101,
src/services/base/RemoteService.ts:457, setting.const.defaults.ts:153 at
484c164450d61deb2e301582b60cb9b5f9fa977f.

[GOTCHA] Published wheels 0.1.57 binary/base64.js:367-408 strips CR/LF before
canonical base64 detection. Upstream compressed binary round-trips can therefore
lose line breaks from plain base64-looking text. The peer treats CR/LF as text.
Blob USVString conversion also replaces unpaired surrogates; the peer leaves
any surrogate-containing data uncompressed. Blob.text() removes an initial BOM;
the peer leaves leading-BOM data uncompressed too. All devices read plain data
regardless of their compression setting (compress.ts:62-71).

[DECISION] Read already-compressed text with upstream Blob.text() semantics,
including U+FFFD replacement and BOM removal. G2 requirement 1's invalid-UTF-8
refusal conflicts with requirement 4's explicit Blob parity; the later explicit
read-parity requirement governs. Malformed base64/deflate still refuses before
publication with a typed, sanitized error and restore instruction. The peer uses
raw zlib level 8; encoded bytes may differ from fflate 0.8.3 but upstream reads
them identically. The committed oracle executes both directions with verified
published dependencies, including surrogate, CR/LF, BOM and invalid-byte vectors.


[GOTCHA] compressDoc skips already-marked wire inputs. They decode to their
logical value, not their original encoded representation; an identity assertion
on the encoded input would contradict that explicit skip/decode contract.
Malformed marked inputs deliberately refuse rather than mirror the published
base64 decoder's swallowed-error empty-string result.


### 2026-11-25 — G3 legacy parameter correction

[DECISION] Replace blanket nonpositive/negative-minimum guards with
input-dependent progress guards derived from pinned chunks.ts:234-250,
297-313,350-371,418-432,473-491. V1 plain cap zero advances one UTF-16 unit;
negative minima terminate; V3 keeps upstream clamps (527-545). Empty/null/NaN
minimum coercion follows upstream. The former `_number` minimum sign check
(`value >= 0`, S1 G3-owned) is removed from minimum admission, rather than
claiming an unreachable positive-minimum invariant. Complete empirical cell
results live in tests/fixtures/legacy_cells_upstream.json; generated grid
expectations remain in settings_oracle_upstream.json.gz. Existing surrogate
hash-collision refusals protect data; SHA1 supplies the collision-free zero-cap
surrogate lifecycle witness. Historical status above remains unchanged.

Review correction: V2 negative caps also loop on empty grouped sources from
BOM-only Blobs and flushed delimiter tails (chunks.ts:288-309). The supplemental
96-cell upstream fixture and preservation tests cover this guard. Numeric
minimum coercion follows JavaScript StringNumericLiteral, including radix
prefixes and exact Infinity spelling, rather than Python float syntax. NaN
backend witnesses exercise the existing ordinary-tweak mismatch bypass,
matching upstream NaN inequality; no compatibility policy was changed.

A further V2 progress witness covers a complete surrogate pair under a
nonpositive cap: chunks.ts:307-309 grows the initial end through both units.
Exactly one Unicode code point terminates; a subsequent code point stalls.
Plain and non-plain pair inputs have executed upstream oracle vectors and
backend write/read/rename/delete coverage.

Signed-zero correction: V2 plain minimum `-0` divides text length to negative
infinity and therefore terminates (chunks.ts:369). Both numeric -0 and the
string "-0" have pinned upstream grid and full backend lifecycle vectors.
Only positive-zero-coercing minima use the loop refusal.

Oracle provenance: commonlib 484c164450d61deb2e301582b60cb9b5f9fa977f,
Node 26.13.1, octagonal-wheels 0.1.57 and xxhash-wasm 1.1.0. Both published
tarballs were verified against lockfile SHA512 integrity; the WASM ESM was
compared byte-for-byte with its tar member. Journal projection expectations
execute pinned journalRecords.ts:69-108, rather than a Python approximation.

Fixture SHA256:

- `settings_oracle_upstream.json.gz`: `8567dc71e08b61ee7b9143ff3fae4efa45f3eb9ef1854cdb1d310a5ab3156a85`
- `legacy_cells_upstream.json`: `ade7b98c1b6ec764baa5333b4d66d15b5eb2662084b7ff09b1f4c1c6e6224ba3`
- `legacy_bom_cells_upstream.json`: `bb0ac5358eab9f9be95fb7f7fdf331deb6cf4186f3728a89c2da16f66030c7a9`
- `legacy_wire_cells_upstream.json`: `5339fb37457691a88e58fe3c7600759a4e0aa24b4d3d413ea7f02218798da50a`

Mutation acceptance: all 68 in-memory mutations are caught by each selected
unit/grid, CouchDB and journal witness (204 failing mutant tests). No production
module is rewritten for these checks.

Overflowing minimum correction: JSON.parse converts an overflowing numeric
literal to Infinity. The peer accepts it and follows the V1/V2 grouping and
V3 maximum clamp, rather than retaining the former minimum admission error.
Three executed upstream vectors and both backend lifecycles replace the old
minimum-overflow rejection assertions. All previous 5,815 grid vectors and
69 matrix vectors retain identical decoded pieces and IDs.

Validation before commit: full `./venv/bin/python -m pytest tests/ -q` reports
20,029 passed, 7 skipped and 3 subtests passed (229.93 s), compared with the
owner-provided baseline of 15,413 passed, 7 skipped and 3 subtests. The focused
G3/writer/observability/documentation checks report 4,015 passed. All 68 mutation
campaigns pass their three selected witnesses; the additive overflow oracle
check confirms all previous vectors and mutation witnesses remain unchanged.
Worktree-scoped compileall and diff/ignored-file checks pass. Bare compileall
exits zero but reports the environment's absent system python313.zip; no
interpreter installation changes are part of this task. Post-commit full-suite
evidence is kept in the ignored G3 task directory.


### 2026-11-25 — G5: custom connection headers and S3 addressing

[DECISION] Add independent COUCHDB_CUSTOM_HEADERS / S3_CUSTOM_HEADERS text or
JSON configuration and S3_FORCE_PATH_STYLE (true by default). Copy each
plugin dialog's **Custom Headers** field; these local credentials are never
inferred from milestone settings. Validate HTTP token names/control-free
values without echoing names or values. Report presence/count only.

[GOTCHA] commonlib `common/utils.ts:459-469` truncates text values after the
second colon segment; JSON preserves colons. Text duplicate exact-case keys
retain the last value. The native parser fixture records execution outputs
and source hash; it has no published package dependencies.

[DECISION] CouchDB credentials stay within the configured origin and redirects
are not followed. S3 applies headers at botocore before-send after signing and
preparation, including fallback requests (`MinioStorageAdapter.ts:54-100`).
With configured endpoints, forcePathStyle maps to path/virtual addressing;
SDK DNS/IP/bucket rules still apply. Python and JavaScript SDK endpoint edge
cases and non-ASCII header transport are not claimed byte-identical.

[DECISION] Suppress free-form SDK/HTTP logs process-wide when proxy headers
are used; keep typed application diagnostics. Credentials must not escape
through debug tracebacks. See tests/test_custom_headers.py and the G5 evidence
directory for RED/GREEN, mutation and final full-suite verification.


### 2026-11-25 — G6: CouchDB JWT authentication

[DECISION] Support explicit JWT connection configuration for HS256, HS512,
ES256 and ES512, exclusive with Basic auth. Follow executed commonlib
`484c164450d61deb2e301582b60cb9b5f9fa977f`, `src/replication/httplib.ts:67-212`:
header/payload insertion order, Latin-1 btoa, base64 key import, raw EC signatures,
per-backend cache and strict ten-second refresh boundary. Defaults are at
`src/common/models/setting.const.defaults.ts:198-203`; plugin field labels are at
`SetupRemoteCouchDB.svelte:240-297` (plugin pin `5070b67beae3b60e3a79132386a08da3f1ef213a`).

[GOTCHA] The prose task description said trimmed subject and floor seconds;
the pinned code validates trimmed sub but serializes original sub (:150/:171),
and uses signed int32 seconds (:166/:190). Preserve executed upstream behavior,
including whitespace, Latin-1 encoding constraints and post-2038 wrapping.

[REUSE] `scripts/extract_jwt_vectors.mjs` records real published Computed/binary
helpers, verified archive integrity, source digests and synthetic test keys.
No production credentials are fixtures. Diagnostics expose only typed auth mode
and algorithm. PyJWT 2.15.1 and cryptography 50.0.2 are explicit direct pins;
requirements.lock and installed versions already match.

Live JWT startup is env-gated by `COUCHDB_TEST_JWT_URL`. The URL was absent in
this task environment, so live CouchDB acceptance remains NOT DONE; the HTTP
fake and WebCrypto-generated wire fixtures cover offline behavior.


### 2026-11-25 — G4a whole-note text budget

[DECISION] Apply one decoded UTF-8 text budget to plain documents and textual
newnotes, in direct tools and scans. Explicit nonnegative operator bytes win;
0 is unlimited. Otherwise resolve max(positive vault MiB * 1024 * 1024 floored,
256 MiB) against the current backend size policy. The binary64 conversion
matches commonlib `services/base/VaultService.ts:73-81`. Budget refusals expose
only typed counts, a closed reason and an actionable variable/download remedy.
Regex replacement output defaults to the effective text budget; configure its
bytes independently and configure regex deadlines (default 10 seconds).

[DEFER] P-29 second half: complete metadata index, scan time budget, cursors,
persistent regex worker, fetch line windows and search page limit. Keep current
10000-note / 64 MiB aggregate limits and per-note regex processes.


### 2026-11-25 — G4b complete scans and continuation

[DECISION] Remove default aggregate note/byte caps (0 = unlimited), retain
operator overrides, and add a 20-second configurable aggregate/index time
budget including scan-slot waiting. Use typed stop reasons and opaque cursors
after fully processed display paths, with search match limits and source-note
page limits for other growing aggregate tools. List responses gain a results
and scan envelope. Backlinks, hubs and orphans describe scanned pages only.

[DECISION] Cache incomplete CouchDB metadata progress by update_seq and resume
later calls, retaining compact metadata only. Expose typed index completeness
and progress alongside scan budgets; partial counts remain lower bounds.
Upstream commonlib serviceModules/ServiceFileAccessBase.ts:287-290 returns all
files, and pouchdb/LiveSyncLocalDB.ts:481-536 enumerates database pages.

[GOTCHA] Cold index enumeration follows document IDs, which need not sort like
display paths. An incomplete index can discover a path behind an earlier cursor
and lack graph-resolution targets until they are indexed. Finish indexing and restart pagination before relying on complete coverage.
Concurrent creations/renames behind any cursor can also be missed or repeated.

[DECISION] Fetch windows are a transport projection after whole-note text
validation; omitted bounds preserve the prior response shape. Reuse one
isolated persistent regex worker with hard request deadlines, kill/restart on
failure, and synchronous lifecycle cleanup. Per-note search timeouts continue
scanning; replacement timeouts refuse before publication. Negative text-verdict
cache capacity defaults to 65536. No upstream wire-format fixture changes.


### 2026-11-25 — G4b regex validation deadline

[GOTCHA] A scan can spend its remaining aggregate time while validating a
regex before any note is admitted. Return a resumable empty time page when
the aggregate clock expired; retain the regex-specific timeout refusal when
its own deadline expires first. Neither path echoes the pattern or body.

### 2026-11-25 F1-1

[DECISION] Compare journal publication collisions after transport projection; preflight lossless CouchDB collisions and name setting-level recovery. Upstream: journalRecords.ts:69-108; EntryManagerImpls.ts:133-159; DatabaseWriteLayer.ts:24-49.

### 2026-11-25 F1-2

[DECISION] Execute ContentSplitterBase.ts:68 coercion for every JSON type; retain V1 loop protection and V2 binary NaN data-loss refusal. Coercion oracle records real splitter/hash dependencies and source hashes.

### 2026-11-25 F1-3

[DECISION] Use original-type += at chunks.ts:368-370 for V2 and V2-segmenter. Remove the integer shortcut altogether: after correct fallback semantics its numeric-only modulo mutation would be equivalent. Replace the false proof with executed [1]/[2] coercion cells; the historical modulo probe is obsolete, not a claimed kill.

### 2026-11-25 F1-4

[DECISION] Add typed successful-publication notices for legacy BOM stripping, opt-in malformed UTF-8 decoding, Blob USVString and compact-journal surrogate replacements. Original result text remains verbatim. P-11 verified at EntryManagerImpls.ts:280 and reflection.ts:272-283: default receiving devices refuse the resulting nonzero size mismatch; processSizeMismatchedFiles bypasses it.

### 2026-11-25 F1-5

[DECISION] Route the compatibility count wrapper through the shared external notice suffix; remove the obsolete count collector. The gate object-valued-message witness retains all original fields.

### 2026-11-25 F1-6

[DECISION] Remove operator-style ignore-name validation for both sources; match split/JS-trim/lookup semantics from targetFilter.ts:95-145 and path.ts:241-265. Inactive names never gate startup. Restrict actual storage lookup to note identifiers; report unresolved explicit names as a typed warning count.

### 2026-11-25 F1-7

[DECISION] Re-resolve the effective milestone size policy at the journal refresh boundary through TextAccessPolicy. Retain explicit size/text overrides. A 300.25 to 350.5 MiB change now raises the automatic text budget from 314834944 to 367525888 bytes without restart; VaultService.ts:73-81 defines the device predicate.

### 2026-11-25 F1-8

[DECISION] Accept zero JWT minutes and apply the five-minute fallback in JWTTokenGenerator, matching httplib.ts:167. Negative durations name the real already-expired-token constraint. Regenerated native WebCrypto oracle includes zero-duration HS/ES vectors at fixed time with integrity-verified published wheels.

### 2026-11-25 F1-9

[DECISION] Inject typed timeouts at the tool boundary while retaining real isolated-worker lifecycle tests. Replace sleep-based cancellation/scan coordination with events; widen scheduler-sensitive smoke-test margins. A 200 ms process-start-delay probe reproduced the gate failure before the change and passes afterward. Gate completion requires two concurrent full-suite runs plus a final post-commit suite.

### 2026-11-25 F1 independent-review follow-ups

[DECISION] Keep replacement evidence for either collision order; refresh unresolved-name warnings from current lookups. Extend executable coercion evidence to own toString fields, lexical numeric overflow, empty/BOM inputs, NaN-cap/minimum interaction and zero strings whose first += becomes NaN. Preserve V1/V3 evaluation order and refuse negative-infinite V2 caps. Use closed typed diagnostics so all recovery instructions reach the caller. The obsolete integer modulo branch is removed; no claim that its historical equivalent mutant was killed.

### 2026-11-25 F1-4 reserved-prefix audit

[DECISION] Extend the same typed notice mechanism to the documented CouchDB reserved-prefix interpretation (compress.ts:49-71; RemoteService.ts:457). A valid 40-byte oracle piece reads as 1300 decoded bytes under either write setting; disclose this without changing IDs, wire data or metadata size. Refuse undecodable new prefixes before any mutation, with binary-attachment recovery; compare logical reader values for valid repeat publication. The unchanged compression fixture supplies all decoded expectations.

### 2026-11-25 F1 final gate verification

[DECISION] Both independent code/architect reviews converged without new findings across F1-1 through F1-9 and the reserved-prefix ingestion follow-up. Two concurrent exact full suites on dc7e21b each passed 22,899 tests, with 8 skipped and 3 subtests (348.53 s / 349.41 s). The owner-supplied F1 baseline was 20,611 passed, 8 skipped, 3 subtests. All 27 final process-local own mutation variants were caught; effective reviewer mutations were caught with adapted targets where necessary. The historical integer-modulo target was removed, so its original probe is inapplicable rather than claimed killed. Working logs and the detailed report remain ignored in the F1 task directory; no live production/device verification was performed. A final full suite follows this status commit.

### 2026-11-25 F2-7 — JWT cache boundary

[GOTCHA] Numeric zero uses five minutes for both signing and cache expiry; explicit empty expiry is refused. Upstream literal string "0" produces a zero-second token and is outside the numeric environment representation. Regression covers all four algorithms through +290.99 / +291 seconds and detects the raw-zero cache mutant.

### 2026-11-25 F2-5 — atomic policy refresh

[DECISION] Resolve selection, size and derived text budgets before installing effective fields. A refused candidate preserves the previous tuple and retains a typed policy failure until repaired. The independent refresh reproducer failed before and passes after the fix.

### 2026-11-25 F2-6 — inactive ignore settings

[DECISION] Cover both backend startups with conflicting and wrongly typed inactive ignoreFiles maps; require successful writes and zero ignore-name reads. The inactive-validation mutant is caught.

### 2026-11-25 F2-1 — repeated minimum concatenation

[DECISION] Add twelve native V2 witnesses including 401 lines and third/fourth concatenation neighbours, using the existing integrity-verified published dependencies. Reviewer double-left and additional third/fourth-only process-local mutants are detected; production splitter remains unchanged.

### 2026-11-25 F2-2 — verbatim publication result

[DECISION] Assert complete original string prefixes before notice suffixes, including whitespace, blank lines, multiline JSON and non-JSON text. The process-local strip-result mutant is detected; non-string identity is retained.
