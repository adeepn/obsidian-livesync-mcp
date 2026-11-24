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
existing aggregate limits. Existing file-type restrictions still apply.

Regex replacement output uses `LIVESYNC_REGEX_MAX_OUTPUT_BYTES` (default 0 =
effective text budget); regex search/replacement deadlines use
`LIVESYNC_REGEX_TIMEOUT_SECONDS` (default 10). Size and timeout refusals are
separate and name the remedy variable. Literal replacement has no regex cap.
Vault statistics expose numeric `text_budget_bytes`, `regex_max_output_bytes`
and `regex_timeout_seconds`. See the [precise matrix](../README.md#text-access-tool-matrix)
and [scan policy](scan-policy.md).
