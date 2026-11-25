# Frontmatter properties and aliases

LiveSync syncs the note bytes without parsing YAML: pinned commonlib
`managers/EntryManager/EntryManagerImpls.ts:253-280` constructs a Blob, then
chunks it. Its file-size predicate is `services/base/VaultService.ts:73-81`.
YAML property projection is an additional peer tool operation, not a vault
admission requirement or a replacement for Obsidian's host YAML parser.

The peer resolves scalar, sequence and mapping anchors and aliases. Plain
container conversion is memoised; repeated references share the same converted
container. Round-trip edits replace only changed top-level key/value token spans, retaining
untouched source slices, including
anchor names, aliases, flow punctuation, comments and scalar spelling. The
candidate is parsed again before publication. An edit that removes a referenced
anchor and would leave an untouched alias dangling declines rather than write
invalid YAML. Edit the raw note text to change those linked keys together.

| Environment variable | Default | Scope |
| --- | --- | --- |
| `LIVESYNC_FRONTMATTER_MAX_NODES` | 1000000 | Expanded property-tree nodes, including mapping keys and repeated alias subtrees |
| `LIVESYNC_FRONTMATTER_MAX_DEPTH` | 200 | Property nesting depth, with the root at zero |

Both overrides are positive integers. Restart after changing them. The YAML
parser also has a real Python interpreter recursion ceiling; raising the
configured depth cannot eliminate that separate runtime constraint. Such a
refusal identifies depth and directs the caller to the raw note text.

A streaming YAML event preflight computes each completed anchor's expanded
size and height. Alias references add those counts without materialising their
subtrees; an active anchor reference identifies a cycle. It declines expansion
bombs before constructing the round-trip YAML tree. Conversion applies the
same limits to programmatically supplied containers and detects cycles.

Refusals name `nodes`, `depth`, `alias expansion`, `invalid YAML`, or
`mapping required` for valid YAML whose root is not a property mapping. They state
that the note itself is intact and synced, that only properties/tags tools
declined, and how to fetch/edit raw note text. Finite budget refusals name the
override. Recursive aliases have no finite JSON representation and must be
changed through raw-note editing. Parse errors never include YAML source,
exception text, property values or remote data. Before creating a new note,
the refusal instead states that creation declined before publication and no
note was created; it never claims an unpublished note was synced.

`tests/fixtures/frontmatter_admission_upstream.json` records native plugin size
admission for the Task D reproducers, scalar/sequence/mapping aliases, alias
chains/cycles, a billion-laughs shape, pathological depth and a near-million-node
note. Its generator executes the pinned method with Node builtins only and
records source/generator digests. It does not claim the plugin parses YAML.
The property values and safety assertions in `tests/test_frontmatter_limits.py`
are the peer JSON-tool contract.

The round-trip loader shares immutable implicit YAML tags and omits nested
sequence per-item coordinate dictionaries. Comments, styles, scalar spellings
and anchor handling remain the pinned `ruamel.yaml` constructor's work; edits
locate source coordinates in the YAML event stream. This avoids retaining
location metadata that property tools never read.

## Measured resource envelope (2026-11-25)

Fresh Python 3.13 subprocesses on the development Mac, default budgets:

| Operation/input | Outcome | Wall time | Peak process RSS |
| --- | --- | --- | --- |
| Read 999997 repeated zero items (1000000 expanded nodes total) | Accepted | 41.870 s | 1085194240 bytes |
| Read 999997 distinct integer items (1000000 expanded nodes total) | Accepted | 45.474 s | 771342336 bytes |
| Replace an existing scalar key beside 999995 repeated list items (1000000 nodes total) | Accepted | 139.656 s | 1723285504 bytes |
| Read depth-199 nested list (scalar at depth 200) | Accepted | 0.029 s | 18399232 bytes |
| Read billion-laughs alias graph | `alias expansion`, before round-trip construction | 0.0015 s | 19365888 bytes |
| Add tags to billion-laughs alias graph | `alias expansion`, before round-trip construction | 0.0017 s | 18595840 bytes |

These are process peaks and measured cases, not portable memory guarantees.
The million-node default bounds materialisation but can require approximately
1 GiB for one projection. The measured near-limit edit peaked at 1.61 GiB;
provision at least 4 GiB headroom per concurrent near-limit edit, plus the
server baseline, or lower the node budget on a smaller host. Fetch/edit raw text when
property projection is inappropriate. The default is a property-tool safety
budget, never a LiveSync file or vault size maximum. Alias expansion uses
source-sized memory for the preflight and never allocates the exponential tree.

The compact round-trip hooks are verified against ruamel.yaml 0.18.17. Both
requirements.txt and requirements.lock pin that release; validate those hooks
and resource measurements before upgrading it.
