# Vault format and text representation

Storage follows LiveSync device **disk ingestion**, with case-sensitive suffixes:
`.md .txt .svg .html .csv .css .js .xml .canvas` use host text reading and
`plain` chunks. Every other path (including `.MD`, `.json`, `.base`, `.yaml`,
`.png`, and extensionless files) uses `newnote` with independently base64-encoded
pieces, even when the content is textual. There is no content sniffing. Both
backends accept strings or bytes: strings for a newnote path become UTF-8 bytes;
bytes for a plain path must be valid UTF-8 or the write is refused before
publication. Upload callers may explicitly set `lossy_text=true` to replace
invalid bytes with U+FFFD; the response reports `replaced_bytes`, stored size,
and stored SHA-256. Newnote bytes remain unchanged. Native-host invalid-byte
behavior still needs live validation; opt-in replacement is this peer's policy.
The existing vault splitter determines pieces and their ordered child IDs;
metadata size is the ingested Blob byte length before splitting, including an
initial BOM. Legacy plain splitters remove that BOM from pieces while keeping
the original metadata size. Reads accept this mismatch, as the device local
database does. Device file reflection can refuse nonzero size mismatches unless
`processSizeMismatchedFiles` is enabled.

Tools expose a separate text representation: eligible newnotes can be fetched
and edited as text, regardless of extension. Their bytes must pass strict
UTF-8, control-character and per-file byte-budget checks.
Edits go through the same path-based writer, so `.base`/`.json`/`.yaml` stay
newnote. Frontmatter and canvas tools retain their existing format checks.
Ineligible newnotes return metadata (`size`, `sha256`) from `obsidian_fetch`;
use `obsidian_download_attachment` for their bytes. Newnote files that decode
as UTF-8 can also be downloaded without conversion.

Uploads use the vault's device size policy (see [Size behavior](size-policy.md)).
Their response size/SHA-256 describe the stored bytes; plain-path decoding and
splitting can change the input bytes. `obsidian_download_attachment(path,
max_bytes=0)` returns complete JSON `{path, size, sha256, content_b64}` over the
stored raw bytes. There is no peer file-size cap on downloads. A positive
`max_bytes` is an explicit caller limit: exceeding it returns an error without
content or truncation. Plain documents use `obsidian_fetch`.

The server derives document ids from vault paths the same way LiveSync does.
Both backends support these vault settings:

- **No end-to-end encryption** — E2EE vaults are rejected at startup (and encrypted chunks are refused on read).
- **No path obfuscation** — obfuscated vaults are rejected at startup.
- **Either filename case setting** — document ids preserve case when
  `handleFilenameCaseSensitive` is on and are lowercased otherwise (the default).
- **Path normalization version 0 or 1** — legacy spelling (the default), or
  Unicode NFC, independently of the case setting.

A vault using an unsupported combination fails the startup check with an explicit error instead of serving mismatched data.

Writes support all pinned dispatch versions: v1, v2, v2-segmenter
(identical to v2 at this pin), v3-rabin-karp and legacy empty-version migration.
`minimumChunkSize` defaults to 20; `customChunkSize` defaults to 0.
`longLineThreshold` is unused by the selected algorithms. V3 text at least
4 MiB uses binary thresholds while retaining text pieces; valid UTF-8 cuts can
exceed the cap by up to three bytes. Binary pieces are independently base64
encoded. Empty persisted content has no children; duplicate pieces retain
ordered references. Existing chunks remain readable under their original IDs.
`backend_info` reports `writer_supported` and `writer_unsupported_reason`.

## Path admission

Ordinary tools reject service prefixes `i:`, `ix:`, `ps:`, `h:`, `f:`;
first segments `_design`/`_local`; exact service names
`obsydian_livesync_version`/`syncinfo`; empty paths and any dot-prefixed
segment. Namespace checks are case-insensitive. Hidden ignore files have a
separate internal read path; this does not enable public Hidden File Sync.
See `livesync_format.validate_vault_path`. Select an ordinary path for MCP
writes; internal metadata is not a public note interface.

## Invalid writer parameters

The derived pieceSize is `floor(102400 * ((customChunkSize || 0) * 1 + 1))`;
`customChunkSize = -1` gives zero. Negative minima are supported. Empty string,
null and NaN minima follow JavaScript coercion, not peer numeric validation.
Overflowing numeric minimum literals become Infinity and follow the same
upstream grouping and clamp rules.
V3 retains its 30 KiB cap floor and minimum/maximum clamps, including NaN's
suppression of content-defined candidates (finite maximum cuts still apply). A NaN cap suppresses all V3 boundaries.

The following guards refuse actual upstream nontermination. Separate data-loss and primitive-coercion guards are described below:

| Algorithm and input | Loop condition | Recovery |
| --- | --- | --- |
| V1 nonempty plain text | Negative or NaN pieceSize; substring cannot consume the buffer | Use customChunkSize producing a positive pieceSize; zero also terminates |
| V1 nonempty non-plain text or binary | Nonpositive or NaN pieceSize; remaining text or byte offset does not advance | Use customChunkSize producing a positive pieceSize |
| V2/V2-segmenter nonempty binary | Nonpositive pieceSize; splitEnd never advances | Use customChunkSize producing a positive pieceSize |
| V2/V2-segmenter text | Negative-infinite pieceSize (even a single code point), or finite nonpositive pieceSize with a group containing more than one Unicode code point (from stalls at the first code point boundary), or negative pieceSize with an empty group (surrogate-growth loop never ends) | Use customChunkSize producing a positive pieceSize |
| V2/V2-segmenter nonempty decoded plain text | The first original-type += preserves positive zero (ordinary 0/null/false, zero-only or whitespace-only primitive strings); decimal/radix/exponent zero strings becoming NaN and negative zero terminate | Use a positive minimumChunkSize |

Empty Blobs retain the tested primitive-parameter behavior; a noncallable own toString can throw during required coercion. V2 text groups containing
a single code point (including a surrogate pair) terminate with a finite
nonpositive cap. V1 plain zero-cap splitting emits
one UTF-16 unit per piece, intentionally splitting surrogate pairs. Different
lone surrogates can share an xxhash ID after TextEncoder replacement; the
existing collision guard protects shared data. Use a collision-free hash mode
(such as SHA1 for the committed surrogate witness) or a positive pieceSize.

These rules follow commonlib@484c164 `chunks.ts:234-250,297-313,350-371,
418-432,473-491,527-545`. `scripts/extract_legacy_cells.mjs` commits all 864
cells in `tests/fixtures/legacy_cells_upstream.json`: eight dispatch/migration
choices × three input classes × three cap signs × six minima × empty/nonempty.
Each looping cell has a source progress proof and an isolated upstream child
with a 128-yield limit and a 2-second wall deadline. Defined pieces and IDs
come from pinned upstream execution with integrity-verified published wheels.
The supplemental `scripts/extract_legacy_bom_cells.mjs` and
`tests/fixtures/legacy_bom_cells_upstream.json` record 96 BOM-only and newline
probes, including empty decoded sources and final delimiter tails. JavaScript
radix-prefixed numeric strings, signed zero and single-surrogate-pair groups
are also covered by the main oracle. `scripts/extract_legacy_wire_cells.mjs`
executes the pinned journal encoder/decoder for zero-cap surrogate pieces in
`tests/fixtures/legacy_wire_cells_upstream.json`. NaN is
encoded as a tagged input recipe in JSON and materialized as a real NaN in
upstream execution and backend tests; ordinary tweak equality treats NaN as
unequal to itself, so those backend tests use the existing explicit
`LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH` bypass. No defined upstream
validation errors occur in these tables.

## CouchDB JSON string encoding

Uncompressed CouchDB requests use compact JSON with raw UTF-8 for non-ASCII
characters, matching PouchDB HTTP adapter 9.0.0 `lib/index.es.js:765-773`
(`JSON.stringify(doc)`). Well-formed JSON can escape an unpaired UTF-16 unit,
but CouchDB 3.5.3 rejects that document with HTTP 400, “invalid UTF-8 JSON”.
The peer therefore refuses a complete publication containing such a unit
before sending any leaf; a device-shaped request is rejected by that server too.
Legacy V1 can split an emoji pair (`chunks.ts:243-252`); choose a surrogate-safe
splitter such as v3 rather than expecting later leaf joining to repair the
server rejection. Chunk IDs are unchanged. No replacement publication occurs
and CouchDB has no `transport_replacements` count. The journal instead mirrors
TextEncoder replacement; see [surrogate wire encoding](s3-journal.md#surrogate-wire-encoding).


## CouchDB compression

The remote transform is commonlib `src/pouchdb/compress.ts:9-101`, installed by
`src/services/base/RemoteService.ts:457` at commit
`484c164450d61deb2e301582b60cb9b5f9fa977f`. It covers every string `data` field,
including legacy/ordinary notes, newnote documents and leaf chunks. Every read
recognizes `\u000ELZ\u001D` regardless of the local compression setting.
Strip that marker; optional `~` selects binary output, followed by base64 of a
raw DEFLATE stream. Inflate bytes, then either return canonical base64 (`~`)
or decode as Blob.text(): UTF-8 with U+FFFD replacement and leading BOM removal.
An empty payload (also `~` alone) yields empty text. Non-string data is unchanged.
Invalid base64 or deflate raises a sanitized `CompressedDataError` carrying only
a closed recovery instruction and cause class; restore the document from a
healthy device or backup. No corrupt marked body is returned or republished.

Writes follow boolean `enableCompression` from PREFERRED, otherwise the first
readable milestone settings map; absent means false. Non-booleans refuse:
`enableCompression must be a boolean`; correct the vault setting and restart.
Conflicting device values are safe and do not require alignment. Already-marked
values are encoded wire inputs and pass through; their decoded logical value,
not the marker-prefixed representation, is returned on read. The byte-exact
compress/decompress identity applies to unmarked source inputs. Compression replaces data only when the marked result is
shorter in UTF-16 units. Devices use fflate 0.8.3 level 8; the peer uses zlib raw
deflate level 8. The encoded bytes may differ; both are device-readable streams.
Journal storage is unchanged and keeps compression forced off.

Published octagonal-wheels 0.1.57 `dist/binary/base64.js:367-408` detects canonical
base64 after stripping CR/LF. The peer deliberately treats any CR/LF value as
text: upstream's binary path silently removes line breaks. Without CR/LF the
base64/text decision matches upstream. CouchDB 3.5.3 rejects lone-surrogate JSON escapes with HTTP 400
`{"error":"bad_request","reason":"invalid UTF-8 JSON"}` (verified 2026-11-25).
The backend checks every document string before publication and refuses the
whole write with `WriterParameterRefused`. V1 non-plain cuts can isolate UTF-16
units (`chunks.ts:243-252`); use v3's protected text boundaries
(`chunks.ts:698-699`) or avoid synchronizing the note. Devices fail too.
Paired JSON escapes and ordinary UTF-8 emoji are accepted. Journal leaves still
use the upstream TextEncoder replacement; JSON serialization is unchanged. Leading-BOM text is also uncompressed, since Blob.text()
would strip that BOM on read. Every device accepts these uncompressed values.
Existing compressed values retain upstream decoding losses; those bytes cannot
be recovered by a reader. These exceptions preserve the caller's data rather
than imposing a compression requirement on interoperable uncompressed pieces.

Regenerate the offline oracle with Node 26.13.1+:

```bash
node scripts/extract_compression_vectors.mjs /tmp/settings-parity-commonlib /tmp/task3b-wheels /tmp/task3b-wheels.tgz /tmp/g2-fflate-0.8.3.tgz tests/fixtures/compression_upstream.json
```

The generator refuses changed pinned sources or dependency tarballs. Its fixture
records source SHA-256, lockfile dependency versions/integrities and Node version;
real upstream reads both fflate and Node zlib streams, including invalid UTF-8.


## CouchDB request headers

`COUCHDB_CUSTOM_HEADERS` supplies custom headers on every request to the
configured database server, including metadata, documents, attachments and
`_local` documents. Absolute URLs to other origins are refused and redirects
are not followed. Like normal device replication
(`RemoteService.ts:360-369`), custom values
append to existing headers, including the Basic Authorization header.
Headers do not change the
stored vault format. See [proxy configuration](connecting-storage.md#reverse-proxies-and-auth-proxies)
for text/JSON forms, validation, typed count diagnostics and secret handling.
Upstream: `LiveSyncReplicator.ts:2071-2098`, `diagnostics/couchdb.ts:45`.

Journal within-publication collisions compare TextEncoder-projected data; identical projected pieces retain all original child IDs. CouchDB preflights distinct colliding pieces before any mutation; configure a positive customChunkSize or a collision-free hashAlg such as sha1.

customChunkSize follows `(value || 0) * 1 + 1` and Math.floor exactly for JSON numbers, numeric/radix/exponent strings, booleans, null, arrays and objects. Nonnumeric caps are admitted where text/V3 splitting terminates; a V1 loop or V2 binary data loss refuses with guidance to set customChunkSize=0 or another numeric value.

minimumChunkSize remains its original JSON type during the V2 / V2-segmenter adjustment loop (chunks.ts:368-370): [1] becomes "11", and subsequent additions concatenate. Numeric primitives use binary64 repeated addition. V1 has no adjustment loop; V3 uses Math.min/max coercion. The integer shortcut and its invalid array-equivalence proof have been removed.

Legacy Blob.text strips a leading BOM while metadata keeps Blob.size (EntryManagerImpls.ts:280). Incoming reflection.ts:272-283 rejects a nonzero size mismatch unless processSizeMismatchedFiles is enabled. Every successful lossy peer publication now appends typed notices; V3 retains the BOM.

JWT duration follows httplib.ts:167 `(jwtExpDuration || 5) * 60`: COUCHDB_JWT_EXPIRY_MINUTES=0 uses five minutes. Negative durations refuse because the upstream token would already be expired.

JSON coercion edge recovery follows the executed device: an own noncallable toString field cannot produce a primitive; remove that field or use a primitive for the named setting. JSON lexical overflow inside arrays retains JavaScript Infinity spelling. V3 evaluates its minimum even for empty input or a NaN cap; V1 empty ordinary text also evaluates its minimum. V2 admits zero strings such as "0.0", "+0", " 0 ", "0x0" and "1e-1000" when += becomes NaN, while zero-preserving addition refuses the actual loop. A negative-infinite V2 cap loops even on one code point. All refusals reach MCP as closed typed diagnostics naming the setting and recovery, without setting values.

### Literal reserved compression prefixes

compress.ts:49-71 passes already-marked pieces through on writes and decodes them on reads; RemoteService.ts:457 installs that reader independently of the write setting. A new literal prefix therefore returns decoded content, with the original metadata size retained; `couchdb_marker_interpreted` discloses this through typed notices. Undecodable new prefixes refuse before any publication: remove the prefix or use a binary attachment path. A binary/newnote path preserves literal bytes as base64 chunks. Valid repeat/fresh-peer writes keep original IDs and compare existing logical decoded values; shared-leaf guards remain active.

JWT expiry input is numeric: an explicit empty `COUCHDB_JWT_EXPIRY_MINUTES` is refused; omit it or use `0` for the five-minute fallback. The upstream literal JSON string `"0"` is truthy and gives a zero-second token; this numeric environment variable cannot represent that string. For numeric 0 and 5, all four algorithms retain the cached token through +290.99 seconds and refresh at +291 seconds (httplib.ts:166-167,181-196).

The V2 minimum-concatenation oracle includes the 401-line `abcd\n` witness with minimumChunkSize=[1], plus boundary neighbours through the fourth adjustment. Exact native pieces and IDs protect repeated string addition, not only the initial [1] to "11" conversion.

Existing shared journal content is checked in both compact chunk files and full-JSON leaves before publication. Conflicting or encrypted leaves refuse without replacing data: inspect and resolve the conflicting leaf in Obsidian, or coordinate an alternate hashAlg or a positive customChunkSize before retrying. Diagnostics contain fixed recovery text and no note bytes or remote values.

Splitter parameter arrays use the shared iterative scalar conversion, including native null/undefined empty fields and active-path cycle handling; JSON admission and decoder capacity remain separate ingress constraints. See [size policy](size-policy.md).
