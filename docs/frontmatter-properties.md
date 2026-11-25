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
| `LIVESYNC_FRONTMATTER_MAX_NODES` | 20000 | Expanded property-tree nodes, including mapping keys and repeated alias subtrees |
| `LIVESYNC_FRONTMATTER_MAX_BYTES` | 4194304 | Expanded UTF-8 string/key bytes at every occurrence, before serialization |
| `LIVESYNC_FRONTMATTER_MAX_DEPTH` | 200 | Property nesting depth, with the root at zero |

All overrides are positive integers. Restart after changing them. The YAML
parser also has a real Python interpreter recursion ceiling; raising the
configured depth cannot eliminate that separate runtime constraint. Such a
refusal identifies depth and directs the caller to the raw note text.

A streaming YAML event preflight computes each completed anchor's expanded
size and height. Alias references add those counts without materialising their
subtrees; an active anchor reference identifies a cycle. It declines expansion
bombs before constructing the round-trip YAML tree. Conversion applies the
same limits to programmatically supplied containers and detects cycles.

Refusals name `nodes`, `bytes`, `depth`, `alias expansion`, `invalid YAML`, or
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

Fresh Python 3.13 subprocesses on the development Mac, historical F6 budgets (superseded below):

| Operation/input | Outcome | Wall time | Peak process RSS |
| --- | --- | --- | --- |
| Read 999997 repeated zero items (1000000 expanded nodes total) | Accepted | 41.870 s | 1085194240 bytes |
| Read 999997 distinct integer items (1000000 expanded nodes total) | Accepted | 45.474 s | 771342336 bytes |
| Replace an existing scalar key beside 999995 repeated list items (1000000 nodes total) | Accepted | 139.656 s | 1723285504 bytes |
| Read depth-199 nested list (scalar at depth 200) | Accepted | 0.029 s | 18399232 bytes |
| Read billion-laughs alias graph | `alias expansion`, before round-trip construction | 0.0015 s | 19365888 bytes |
| Add tags to billion-laughs alias graph | `alias expansion`, before round-trip construction | 0.0017 s | 18595840 bytes |

These are process peaks and measured cases, not portable memory guarantees.
The former million-node default bounds materialisation but can require approximately
1 GiB for one projection. The measured near-limit edit peaked at 1.61 GiB;
provision at least 4 GiB headroom per concurrent near-limit edit, plus the
server baseline, or lower the node budget on a smaller host. Fetch/edit raw text when
property projection is inappropriate. The default is a property-tool safety
budget, never a LiveSync file or vault size maximum. Alias expansion uses
source-sized memory for the preflight and never allocates the exponential tree.

The compact round-trip hooks are verified against ruamel.yaml 0.18.17. Both
requirements.txt and requirements.lock pin that release; validate those hooks
and resource measurements before upgrading it.

F7 lowers the default to 20,000 expanded nodes: the confirmation measured a
20,000-node edit at 4.67 s / 55 MB versus 176 s / 1.72 GB at one million.
The 10,001-item regression remains admitted; the explicit override is retained.
Depth 200 remains unchanged; the measured depth-199 case above is small.


F7 also counts string/key UTF-8 bytes at every alias occurrence, including
memoised sequences and mappings, before JSON serialization. Event preflight
conservatively counts scalar spellings too; resolved conversion counts actual
strings and keys (including inherited merge keys). Node limits separately bound
container and punctuation overhead. This is a payload budget, not an exact JSON
wire-length limit or total process memory guarantee; escaping can enlarge JSON.
The 4 MiB default is sixteen times smaller than the confirmation's 67,173,302-byte
JSON amplification (62,403-byte source, 4,099 nodes, 266 MB peak RSS). It retains
large property values while refusing that expansion before round-trip construction.
Raise `LIVESYNC_FRONTMATTER_MAX_BYTES` and restart with sufficient memory, or
fetch/edit raw note text. The raw-note sync path does not apply these budgets.


F7 fresh-process measurements on 2026-11-25 (shared development host):

| Case | Outcome | Seconds | Peak process RSS bytes |
| --- | --- | --- | --- |
| Exactly 4 MiB expanded UTF-8 strings/keys; 2,097,151-byte anchored scalar reused twice | Accepted | 2.614 | 45629440 |
| 20,000 expanded-node existing-key edit | Accepted | 6.815 | 54280192 |
| Reviewer's 62,403-byte scalar-alias document | `bytes`, before construction | 0.031 | 20774912 |

These probe the scalar-byte and node boundaries separately, not every possible
joint worst case. JSON escaping, YAML source size and concurrent requests add
memory beyond the payload budget. The historical F6 4 GiB provisioning advice
above describes the superseded million-node envelope, not the new default.

A combined boundary probe admits exactly 20,000 expanded nodes and 4,194,304
UTF-8 string/key bytes. Editing an existing scalar, reading it and serializing
the properties took 7.102 s with 41353216 bytes peak process RSS; compact source
75981 bytes, JSON 4274298 bytes. This exercises both configured boundaries; it
is not proof of a global worst case over all YAML spellings and comments.
