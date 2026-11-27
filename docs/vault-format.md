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
nonpositive cap. The plugin's V1 plain zero-cap splitter emits one UTF-16 unit
per piece. The peer instead keeps each valid pair together; lone units are
not paired with unrelated characters. See the lossless text boundary rule below.

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
The peer's own valid astral text never creates lone-surrogate leaves. Caller
lone units undergo the existing Blob USVString ingress replacement and
`input_surrogates_replaced` notice; raw lone-unit leaves still refuse in CouchDB.
CouchDB has no `transport_replacements` count. Journal wire projection remains
unchanged; see [surrogate wire encoding](s3-journal.md#surrogate-wire-encoding).

## Lossless text boundaries

The peer extends a nominal text cut by one UTF-16 unit when it falls inside a valid surrogate pair, then starts the next piece at its full nominal size.
This deliberate lossless difference affects V1 non-plain text (`chunks.ts:243-252`)
and V1 plain text with a zero cap (`:234-240`, effective cap one). Grouping,
empty tails and binary partitions are unchanged. Pieces and IDs are byte-identical
to upstream wherever upstream does not split a pair; moved cuts are hashed anew.
V1 positive-cap plain text already protects pairs. V2 and V2-segmenter route both
text paths through `chunkStringGenerator` (`:297-313,350-379`); V3 rejects cuts
before UTF-8 continuation bytes (`:555-556,617-633`). Reads retain stored children
and data, including historical plugin corruption; no repair or re-chunking occurs.
There is no setting or environment switch for this rule.

`scripts/extract_settings_oracle.mjs` and `scripts/extract_lossless_vectors.mjs`
retain raw upstream pieces/IDs alongside independent JS `lossless_pieces` and
`lossless_children` hashed by pinned upstream managers. The compressed
`tests/fixtures/lossless_upstream.json.gz` crosses both text paths, all five hash
modes, zero caps, safe cuts, groups and lone units with source/dependency provenance.



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
whole write with `WriterParameterRefused`. The plugin's V1 non-plain cuts can
isolate UTF-16 units (`chunks.ts:243-252`); the peer extends those cuts to keep
valid pairs together. Raw lone-unit leaves still refuse; restore them from a
healthy device or provide valid text.
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

Existing shared journal content is checked in the stored chunk documents and in files of releases before chunk documents before publication. Conflicting or encrypted leaves refuse without replacing data: inspect and resolve the conflicting leaf in Obsidian, or coordinate an alternate hashAlg or a positive customChunkSize before retrying. Diagnostics contain fixed recovery text and no note bytes or remote values.

Splitter parameter arrays use the shared iterative scalar conversion, including native null/undefined empty fields and active-path cycle handling; JSON admission and decoder capacity remain separate ingress constraints. See [size policy](size-policy.md).

## CouchDB inflate budget

P-4 operational protection applies to every marked document, single or batch,
and reserved-prefix write preflight, independently of the compression setting.
The default is max(32 MiB, base64 size of max(4 MiB, resolved splitter cap) +
UTF-8 boundary headroom, times safety factor 4). This also covers legacy text
UTF-16 units expanding to three UTF-8 bytes each. V3 uses at most 4 MiB plus
three bytes; legacy caps can be larger. Nonfinite caps can emit whole notes
without a finite native maximum: the generous floor remains operational policy,
not a claim of a protocol maximum. Older foreign pieces may also need tuning.

LIVESYNC_COUCHDB_MAX_INFLATED_BYTES accepts a positive integer byte override;
empty derives the default. Raise it and restart for a legitimate larger piece,
or restore a corrupt document from a healthy device. Incremental inflation
bounds each output block and retained output before UTF-8 projection or base64
expansion; projected output also must fit. This is per-document, not an aggregate
batch memory envelope, and is distinct from device file-size admission.


### HTTP response decoding

All CouchDB requests force `Accept-Encoding: identity`, including when custom
headers request compression. This transport-safety policy differs from the
plugin fetch delegation (`C:services/base/RemoteService.ts:340-380`). A server
that still compresses is read through raw HTTPX streaming, with independent wire
and decoded byte caps. gzip (including concatenated members) and deflate (zlib
or raw) use incremental, output-bounded zlib calls; HTTPX never eagerly inflates
the network response. Unknown/stacked encodings, including brotli, and malformed
streams refuse with a typed instruction to configure identity responses.

Each response uses the existing LIVESYNC_COUCHDB_MAX_INFLATED_BYTES budget (or
its splitter-derived default). This includes the entire JSON envelope, not only
`data`. Document/attachment responses, `_local`, `_session`, database info,
`_changes`, id-only `_all_docs`, errors and `_bulk_docs` acknowledgements all
use one budget. `_all_docs` with `include_docs=true` and an explicit local keys
list receives one budget per requested key; remote counts cannot enlarge it.
No `_bulk_docs`, `_changes` or direct attachment endpoint is currently called
by the backend, but the same request boundary protects them.

Default V3 maximum pieces (4 MiB+3), their base64 JSON envelopes and direct
attachments fit the 32 MiB floor; larger finite splitter caps increase the
default as above. Files are transported in chunks, so a 50 MiB file does not
require a single 50 MiB response. Arbitrary legacy pieces, large metadata or
control responses, and tightly configured overrides may need a larger budget.
Raise the named variable and restart for legitimate larger responses. After a
refused write acknowledgement, re-read the note before retrying: the server
may already have committed it. These resource caps do not change file admission.

### Unreadable write acknowledgements

An unreadable write-purpose PUT/POST/DELETE response is outcome unknown, including document,
`_bulk_docs`, `_local`, delete and leaf requests: the change MAY have been applied.
Re-read before retrying; append is NON-IDEMPOTENT. This applies to wire/decoded
budget overflow, unsupported Content-Encoding, corrupt/truncated compression and
connection loss after sending. Budget errors retain the typed byte limit and
LIVESYNC_COUCHDB_MAX_INFLATED_BYTES raise/restart guidance; encoding errors name
Content-Encoding and advise configuring the server/proxy for identity encoding.
No remote header/body values are exposed. Read-purpose GET/HEAD and read-only POST
requests keep typed refusals; connection,
connect-timeout and pool-timeout failures before sending remain unavailability.
Classification is by endpoint purpose, never by HTTP verb alone. The single
`_REQUEST_PURPOSES` table is consulted before sending and controls body failures,
transport failures and readable HTTP status mapping alike:

| Endpoint family | Methods and purpose |
| --- | --- |
| Root info, `_session` | GET/HEAD: read |
| Database (with or without a leading slash) | GET/HEAD: read; PUT/POST/DELETE: write |
| `_db_info` | GET: read |
| `_all_docs`, `_changes` | GET/POST: read |
| `_bulk_docs` | GET: read; POST: write |
| Single document | GET/HEAD: read; PUT/POST/DELETE: write |
| `_local` document (encoded or literal separator) | GET/HEAD: read; PUT/DELETE: write |
| Content-addressed `h:` leaf | GET/HEAD: read; PUT/DELETE: write-leaf |
| Attachment | GET/HEAD: read; PUT/DELETE: write |

Production callers currently issue session/database/document GET, `_all_docs`
GET/POST and document PUT (including local/leaf IDs). The remaining declared
low-level routes retain their established error handling; declarations do not
add public tools. `_design`, `_view`, `_find` and all other undeclared routes or
methods are unused and refused before sending with typed
`CouchDBRequestPurposeRefused`: upgrade the peer to declare the operation before
retrying. This refuses an internal unsupported operation, not a vault setting.
Readable 409 remains a conflict, 404 on GET/HEAD remains caller-interpreted absence,
other readable 4xx remain ordinary refusals (408 on a write remains uncertain),
and readable 5xx is uncertain only for write/write-leaf purposes.

The leaf PUT itself is outcome unknown if its acknowledgement cannot be read.
At the note boundary, `_write_chunk` converts this to MutationNotApplied: metadata
publication has not begun, the note remains unchanged, and an orphan leaf may
exist. It never claims that the leaf was not published. If a move has published
the destination, an unreadable source-retirement acknowledgement retains both
source-removal uncertainty and the typed response recovery details.

This is the peer's data-protection outcome contract, not a new vault restriction
or plugin wire format. The pin's RemoteService.ts:135–159,373–381 observes failures
of body readers after fetch has settled. Accept-Encoding: identity and both
streaming caps remain in force.
