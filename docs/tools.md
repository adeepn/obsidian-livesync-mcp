# Tools

### Notes
- **search** — Full-text search (text/regex)
- **fetch** — Get content, properties, and stats (.md/.base/.canvas)
- **create_note** — Create notes with frontmatter
- **update_note** — Modify (overwrite/append/prepend/after-heading/search-replace)
- **delete_note** — Delete notes
- **move_note** — Move/rename
- **duplicate_note** — Duplicate
- **list_notes** — List files with folder/extension filters

### Properties & Tags
- **manage_properties** — Get/set/delete frontmatter properties
- **manage_tags** — Add/remove/list tags
- **get_all_tags** — All vault tags with counts

### Graph & Links
- **get_links** — Outgoing/incoming links
- **get_link_graph** — Full vault graph
- **find_orphaned_notes** — Notes with no links
- **find_hub_notes** — Most connected notes

### Canvas
- **canvas_node** — Add/remove canvas nodes
- **canvas_edge** — Add/remove canvas edges

### Other
- **obsidian_upload_attachment** — Upload PDFs, scans, and other binary attachments from base64 (vault device size policy)
- **obsidian_download_attachment** — Download complete binary attachments as base64 with size/SHA-256 (optional caller `max_bytes` limit, no truncation)
- **vault_stats** — Vault statistics
- **create_from_template** — Create from template with variable substitution
- **list_templates** — List available templates

## Whole-note text access

Fetch, all update modes, properties, tags, canvas and template reads share
`LIVESYNC_TEXT_SCAN_MAX_BYTES` with text scans, for plain documents and textual
newnotes. Empty resolves to max(positive vault size policy in bytes, 256 MiB);
explicit 0 is unlimited. Within budget they use the whole text. Beyond budget
fetch returns metadata and a typed reason object (`content_available: false`,
`size_bytes`, `budget_bytes`, `reason: text_budget_exceeded`, `remedy`); edits
return the same typed refusal without writing. The remedy names the variable
and complete byte download, which also works for beyond-budget plain documents.
Search/tags/links/graph/statistics put these paths in `skipped` and keep the
optional aggregate budgets and page limits. Existing file-type restrictions still apply.

Regex replacement output uses `LIVESYNC_REGEX_MAX_OUTPUT_BYTES` (default 0 =
effective text budget); regex search/replacement deadlines use
`LIVESYNC_REGEX_TIMEOUT_SECONDS` (default 10). Size and timeout refusals are
separate and name the remedy variable. Literal replacement has no regex cap.
Vault statistics expose numeric `text_budget_bytes`, `regex_max_output_bytes`
and `regex_timeout_seconds`. See the [precise matrix](../README.md#text-access-tool-matrix)
and [scan policy](scan-policy.md).


## Aggregate tool pagination

All tools below accept `cursor=""` and return a `scan` object with `notes`,
`bytes`, `elapsed_ms`, `max_notes`, `max_bytes`, `max_seconds`, and
`stopped_because`. A partial response adds `truncated: true` and `next_cursor`.
Pass that opaque value as `cursor` for the next page. A complete response has
no `next_cursor`. List results use `{ "results": [...], "scan": {...} }` even
when complete; message results use a `message` envelope.

| Tool | Default `limit` | What the limit counts | Page composition |
| --- | --- | --- | --- |
| `obsidian_search` | 200 | Matching notes | Concatenate `results` |
| `obsidian_list_notes` | 200 | Listed source notes | Concatenate `results` |
| `obsidian_get_all_tags` | 200 | Scanned source notes | Sum `tags` counts |
| `obsidian_get_links` | 200 | Scanned source notes | Combine page links; backlinks cover scanned pages only |
| `obsidian_get_link_graph` | 200 | Scanned source notes | Concatenate edges; source out-links are complete once indexing is complete |
| `obsidian_find_orphaned_notes` | 200 | Scanned source notes | Candidates cover scanned pages only |
| `obsidian_find_hub_notes` | 200 | Scanned source notes | Counts cover scanned pages only |
| `obsidian_list_templates` | 200 | Listed source notes | Concatenate `results` |
| `obsidian_vault_stats` | 0 | Scanned source notes | Counts describe this page; inspect index completeness |

`limit=0` disables the response page limit. Aggregate note/byte budgets default
to unlimited; `LIVESYNC_SCAN_MAX_SECONDS=20` includes scan-slot waiting and 0
means unlimited. A busy refusal has typed reason `scans_busy` and retry guidance.
Stop reasons are `notes`, `bytes`, `time`, `index`, `limit`. Static-vault pages
use display paths strictly after the cursor. Notes created or renamed behind
it between pages may be missed or repeated; an incomplete cold CouchDB index
requires finishing indexing and restarting pagination before claiming complete
coverage. See [scan policy](scan-policy.md) for index state and consistency.

## Fetch windows

`obsidian_fetch` adds optional positive 1-based inclusive `start_line` and
`end_line`; either may be omitted. The whole text must first pass
`LIVESYNC_TEXT_SCAN_MAX_BYTES`. Windows use `str.splitlines(keepends=True)` and
return `total_lines`, `returned_lines`, and `truncated`; out-of-range end lines
are clamped. Omit both parameters for the existing whole-note response shape.
Properties and optional statistics still describe the full original note.
