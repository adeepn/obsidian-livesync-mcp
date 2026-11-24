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
