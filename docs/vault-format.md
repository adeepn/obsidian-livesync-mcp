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

V1/V2 require a positive derived piece cap; V2 plain text requires a positive
minimum. Zero can leave the upstream legacy loops nonterminating, so these
configurations are refused. Negative/nonnumeric minimums and other invalid
writer parameters are unsupported. These guard refusals are outside the
“identical for valid settings” claim; V3 retains its own floor/clamp semantics.

## CouchDB JSON string encoding

Uncompressed CouchDB requests use compact JSON with raw UTF-8 for non-ASCII
characters and lowercase `\udXXX` escapes for surrogate code points. This
matches PouchDB HTTP adapter 9.0.0 `lib/index.es.js:765-773`, which sends
`JSON.stringify(doc)`. JSON parsing restores those units without loss; joining
plain leaf data restores pairs separated by legacy splitter boundaries
(commonlib `src/common/utils.ts:83-84`). Chunk cuts and IDs are unchanged.
CouchDB writes have no `transport_replacements` field. The journal's compact
leaf encoding differs; see [surrogate wire encoding](s3-journal.md#surrogate-wire-encoding).


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
base64/text decision matches upstream. Data containing surrogate code points is
published uncompressed: Blob's USVString conversion would replace unpaired
surrogates with U+FFFD. Leading-BOM text is also uncompressed, since Blob.text()
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
