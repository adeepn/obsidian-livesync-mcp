# Size policy and replay budgets

Ordinary writes in **both backends**, including text edits and attachment uploads,
follow the device's `syncMaxSizeInMB`: skip only when `max > 0`, `size > 0`, and
`size > max * 1024 * 1024`. Equality is admitted; fractions work; zero/negative
disables the check. Raising 50 to 100 admits files through 100 MiB. Settings
resolve from milestone `tweak_values.PREFERRED`, otherwise the device maps
(conflicting limits refuse startup), with device default 50 when absent.
A blank/unset `LIVESYNC_SYNC_MAX_SIZE_MB` uses this vault value; an explicit
finite number overrides it. CouchDB resolves at startup; journal peers re-resolve at every unthrottled
refresh. Other selection policies still apply.

Oversized ordinary input is **skipped**, before chunks or metadata are published;
any previous revision stays intact. Byte input is measured before optional
lossy text conversion; strings use Blob UTF-8 size. After conversion, the
Blob size is also checked before publication: lossy replacement may expand
raw input bytes. Either oversized quantity skips the write with previous
revision intact; the new content was NOT saved. Python backends signal `WriteSkipped` as a
control-flow outcome. Every MCP mutation tool handles it as JSON, for example:

```json
{"status":"skipped","path":"scan.pdf","size":43201331,"syncMaxSizeInMB":40,"message":"skipped: 41.2 MiB (43201331 bytes) exceeds the vault's syncMaxSizeInMB (40); new content was NOT saved; raise syncMaxSizeInMB in the plugin settings or pass a smaller file"}
```

This response does not claim success or persistence. MCP has no host vault file
to retain. Copy creates a new file and uses the size gate; delete remains allowed.
Rename with an oversized destination is refused before any mutation: source
revision/content and destination remain unchanged. The error names the size
and vault limit and states that the move was not performed. A device drops the
source from sync after moving its disk file; this peer has no host storage and
would lose the only copy (P-13). A literal same-path rename remains a no-op.
The upstream admission rule is
`C:managers/StorageEventManager/admission.ts:78-108,124-130`.
Received oversized revisions remain stored and readable, and existing DB
revisions are not excluded from journal packing. The plugin can skip reflecting
such revisions into its host vault until its settings allow them; this peer
reads the DB directly. Downloads have no device-size gate; `max_bytes` remains
an optional caller choice. Sources: [parity specification §д](superpowers/specs/2026-11-23-settings-parity.md#д-syncmaxsizeinmb--file-selection-not-journal-admission).

The deliberate size/replay parity difference is **operational protection against
corrupt or hostile journals**. Devices have no corresponding replay budgets.
These budgets are independent of file size and `syncMaxSizeInMB`, including 0:

| Budget | Default | Environment override |
|---|---:|---|
| UTF-8 record line bytes | 256 MiB | `LIVESYNC_JOURNAL_MAX_LINE_BYTES` |
| Inflated journal bytes | 8 GiB | `LIVESYNC_JOURNAL_MAX_INFLATED_BYTES` |
| Records per journal | 1,000,000 | `LIVESYNC_JOURNAL_MAX_RECORDS` |
| JSON nesting levels | 512 | `LIVESYNC_JOURNAL_MAX_JSON_DEPTH` |
| Compressed object bytes (also control objects) | 8 GiB | `LIVESYNC_JOURNAL_MAX_OBJECT_BYTES` |
| Listed keys per refresh | 1,000,000 | `LIVESYNC_JOURNAL_MAX_REFRESH_KEYS` |
| Download bytes per refresh (also controls) | 64 GiB | `LIVESYNC_JOURNAL_MAX_REFRESH_BYTES` |
| Refresh elapsed seconds | 3600 | `LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS` |

Byte, count and depth settings accept positive integers below `2**63`.
`LIVESYNC_JOURNAL_MAX_REFRESH_SECONDS` accepts positive finite numbers below
`2**63`, including fractional seconds such as `0.25`; this preserves the earlier
duration configuration contract. A trip names the budget, observed amount,
configured limit, and exact variable to raise. Streaming inflate/download can
report a lower bound rather than consume an entire hostile body. Restart after
changing env values. Key/byte/time failures preserve completed replay progress;
subsequent throttled calls repeat the actionable diagnostic until a successful
refresh. Increasing budgets permits larger inputs but consumes more resources.

JSON depth has an additional decoder capacity: Python's standard JSON decoder
uses the interpreter recursion limit (normally 1000), minus current call-stack
headroom. Practical nesting capacity is therefore below roughly 1000 with the
default interpreter setting, even if `LIVESYNC_JOURNAL_MAX_JSON_DEPTH=20000`.
The override controls admission, not decoder capacity. Decoder exhaustion is a
`max_json_depth` budget error naming observed nesting, configured budget, exact
variable, and interpreter recursion limit; raising the variable alone cannot
remove that ceiling. Reduce nesting or use a decoder with greater capacity.
The default depth budget of 512 remains independent of file-size policy.

Default derivation: V3 binary pieces are at most 4 MiB; valid text may exceed its
boundary by 3 UTF-8 bytes. Base64 costs 4/3, JSON escaping at worst 6 per raw
byte (compact escaping at most 2). A conservative encoded-piece allowance is
32 MiB: `max(ceil(4 MiB/3)*4, (4 MiB+3)*6) < 32 MiB`. A line gets 8 allowances,
an inflated/compressed journal 256, and a refresh 2048. Metadata lines get the
same large allowance for child references; record/key defaults accommodate many
tiny pieces/objects. One hour permits 64 GiB at about 19 MiB/s; 512 nesting
levels far exceeds ordinary metadata (<10). These are headroom choices, not
protocol bounds. Legacy splitters, foreign peers and journals/history have no
universal maximum: unusually large inputs may require tuning even with these
defaults. No finite budget can promise admission of all unbounded histories.
See [replay_limits.py](../replay_limits.py) for pinned source references.

Journal peers re-resolve the effective size policy at every unthrottled refresh (including refresh(force=True)), and update the derived text budget using the same policy derivation as startup. Explicit LIVESYNC_SYNC_MAX_SIZE_MB and LIVESYNC_TEXT_SCAN_MAX_BYTES overrides remain authoritative, including zero/unlimited.

Journal refresh validates the complete selection, size and derived text policy before committing any effective setting. A refused candidate preserves the previous effective tuple, records a typed failure and blocks writes until the milestone is repaired and refreshed.

Vault-derived syncMaxSizeInMB uses the native JavaScript numeric coercion predicate for every JSON type (VaultService.ts:73-81). Numeric strings and single/nested numeric arrays work; true means a 1 MiB limit. Null, false, nonnumeric strings/arrays/ordinary objects, zero and negative values disable the predicate. Infinity or an overflowing product is unlimited. Objects with an own noncallable toString that cannot produce a primitive refuse with instructions to correct that field. Device maps agree when their coerced integer-byte admission policies agree; conflicting policies require alignment or LIVESYNC_SYNC_MAX_SIZE_MB. The operator override remains a strict finite number.

Journal candidate validation includes the Node selection capability check. While that check awaits, effective selection, size and text limits remain the previous tuple; a missing/unusable Node refusal records a typed failure without committing the candidate. Install Node.js 22+ in the service PATH, then force refresh or restart to recover.
