# S3 journal operation

Set `LIVESYNC_BACKEND=s3` and choose exactly one connection form.
Use the URI or individual-variable block in the quick start matrix.
Prefix is prepended literally to object keys; include a trailing slash for a
folder prefix. Shared state settings work with either connection form.

Before each tool operation, the backend refreshes its local state. On first
use, this replays the full journal history. This one-time replay can take a
long time for a large vault. Startup validates settings and opens local state; it does not replay
journals. Subsequent refreshes use the processed-file ledger. A confirmed remote
wipe clears only deduplication tracking and starts replay from the beginning;
local notes and chunks stay readable. Use a dedicated writable
state directory per vault/peer and persist it across restarts. It contains
`journal.sqlite3` (SQLite revision state and processed journals) and
`chunks/` (cached chunk files).

`obsidian_refresh` lists remote journals and replays unseen files.
`S3_REFRESH_MIN_INTERVAL_SEC` is the minimum spacing between refresh attempts, including failures
and partial budget stops (positive seconds, default 5); calls within that interval are skipped.
Ordinary tool calls refresh automatically using the same throttle. There is
no background polling. `obsidian_refresh` also uses this throttle; allow the
interval to elapse to receive subsequent device changes. Internal callers can use
`refresh(force=True)` to bypass the throttle. Refresh failures
return a tool error instead of serving stale data. Calls throttled after an
unresolved failure also return an error; a successful refresh clears that failure.

Replay protection budgets are an explicit difference from the device, documented
under [Size behavior](size-policy.md). A trip returns an actionable error;
completed files stay processed. Key-limited sweeps continue from their last
listed key, then restart from the beginning so late objects below the cursor
are eventually discovered. Files are numerically ordered within each window.
Byte/time stops retry the unfinished window and skip its processed files.
The time budget is checked between synchronous operations; an in-flight request,
parse or SQLite transaction can finish after the deadline. Configure budgets
large enough for one journal plus control reads. Restarting the server restarts
the listing sweep; the durable ledger preserves progress.

Local state directories and chunk shards are private (`0700`); SQLite and newly
written chunk files use `0600`. Opening an existing cache tightens permissive
directory modes and logs each correction once.

Remote storage is append-only: updates publish new journal objects.
Publication first attempts atomic create-only `PutObject(IfNoneMatch="*")`
(Ruling B-13). Occupied keys are regenerated, with at most ten attempts;
read-back verification still precedes local application. Providers reporting
conditional writes as unsupported use the B-10 exists-precheck + put fallback,
with a warning logged once per adapter. That compatibility fallback retains
the race between the precheck and upload.
The backend never deletes S3 objects, prunes history, or runs remote garbage
collection. Deleting a note publishes a tombstone; it does not remove journal
objects. Automatic dedupe resets retain all local documents and chunks.

Concurrent edits can leave sibling revisions. The winner is deterministic:
highest revision generation, then lexicographically greatest revision hash.
A successful write confirms publication, not that it will remain the winner.
Sibling revisions remain available to LiveSync's conflict UI in Obsidian for
resolution.

Supported S3 vault settings are legacy path identity (normalization version 0)
and NFC identity (version 1), each with case-insensitive or case-sensitive
filenames. Both backends follow the vault setting. NFC aliases composed and
decomposed names even in sensitive mode. Existing local state with another
identity archives the old store in quarantine and starts fresh; incompatible
metadata IDs are refused. E2EE
and path obfuscation remain unsupported.

## Surrogate wire encoding

At commonlib `484c164450d61deb2e301582b60cb9b5f9fa977f`,
`src/replication/journal/journalRecords.ts:69-77` encodes compact leaf records
with `TextEncoder`, replacing each unpaired UTF-16 surrogate unit with U+FFFD.
Metadata records use `JSON.stringify(doc) + "\n"`: non-ASCII remains UTF-8,
and unpaired surrogates become lowercase six-character `\udXXX` escapes.
The reader remains a fatal UTF-8 decoder (`journalRecords.ts:79-108`).
Compression wraps these exact frame bytes in raw DEFLATE.

Legacy non-plain V1 cuts can separate an emoji's surrogate pair
(`src/string_and_binary/chunks.ts:243-252`). V2 `chunkStringGenerator` protects
pairs at `:307-310`; it does not produce this V1 witness cut.
The journal transport then stores two U+FFFD characters in its place on every
device. The peer preserves the original pieces' IDs and follows that same
transport behavior. Its successful write metadata includes the integer
`transport_replacements` only when its publication replaced units; tool text adds:
“The LiveSync journal transport replaces unpaired surrogate units with U+FFFD;
the note is stored exactly as devices would store it.” Counts refer to emitted
compact records, counting each unique chunk once. Metadata escapes are lossless
and do not count as replacements. An ordinary write has no extra field or text.

There is no encoding setting. Re-read the published note to inspect the same
text other devices receive. See the dated upstream [GOTCHA] in the settings
parity spec and `tests/fixtures/surrogate_wire_upstream.json` for executable
upstream frame bytes and decoded records.


## Custom connection headers and addressing

`S3_CUSTOM_HEADERS` accepts plugin **Custom Headers** lines or a JSON object.
These custom headers are applied on every final signed S3 request, including
GET, LIST pages, HEAD, PUT, conditional PUT and exists+put fallback. The
botocore `before-send.s3` hook runs after signing/preparation and can override
any SDK header, matching `MinioStorageAdapter.ts:86-100`. Invalid overrides can
break authentication; remove them or use the proxy's required header names.
`S3_FORCE_PATH_STYLE` defaults to `true`, matching upstream defaults:210 and
the existing peer. Set false for virtual-host addressing. Like upstream
`MinioStorageAdapter.ts:61-67`, this option applies to configured endpoints;
without one, SDK endpoint rules apply. DNS-compatible bucket/host names and
appropriate TLS certificates are required; IPs and incompatible bucket names
may fall back to path style under SDK rules. The Python and JavaScript SDKs
may differ in endpoint resolution edge cases.
See [proxy examples and credential handling](connecting-storage.md#reverse-proxies-and-auth-proxies).
Only typed `custom_headers_configured` and `custom_headers_count` diagnostics
are exposed, never header names or values.

## Transport policy

S3_CONNECT_TIMEOUT_SECONDS=5 and S3_READ_TIMEOUT_SECONDS=30 accept positive
finite seconds; raise and restart for slow healthy storage. S3_MAX_ATTEMPTS=1
accepts a positive integer and includes the initial attempt. Upstream
`MinioStorageAdapter.ts:70-80` configures four attempts. Set S3_MAX_ATTEMPTS=4
to allow that count on list/get/exists and conditional put_if_absent.

| Call | SDK attempt policy | Lost-response consequence |
| --- | --- | --- |
| list / get / exists | Configured count | Safe read repetition; final failure remains unavailable |
| put_if_absent | Configured count | If the first creation succeeded, repetition returns already exists (412); publication allocates another candidate, then verifies read-back. Terminal failures retain outcome unknown because an earlier attempt may have succeeded |
| unconditional put (exists+put fallback) | Always one | Outcome unknown; publication stops without retry; re-read before deciding to retry |

The default stays one because an accepted upload with a lost response is
ambiguous. Hidden unconditional retries could conceal that outcome. Conditional
retries cannot overwrite an existing key, but can leave an extra immutable
journal candidate after a lost response. This does not remove the fallback's
exists+put race on providers without conditional-write support. Custom headers
and timeouts apply to both SDK clients.
