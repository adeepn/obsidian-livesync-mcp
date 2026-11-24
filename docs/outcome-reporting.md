# Outcome reporting

Peer-initiated mutations have three outcomes:

- **Applied:** the backend confirmed durable publication. Journal writes also
  committed local metadata. Success does not guarantee eventual winner status
  against concurrent peers. Continue normally.
- **Refused without change:** policy skips/refusals, explicit CouchDB 4xx
  refusals, and proven connection failures before sending leave the requested
  mutation unapplied. Correct the refusal's cause before retrying.
- **Outcome unknown:** the result begins with `outcome unknown`. The change
  **MAY have been applied**, because the metadata response was lost after
  sending, publication verification failed, or local application failed after
  remote journal publication.

Uncertainty starts only when a request publishing the note's **metadata**
has been sent and its outcome is unknown (P-16). Losing a chunk PUT response
before metadata publication is an ordinary error: the note is unchanged; a
harmless orphan chunk may exist. Copy and move reuse chunks. If move publishes
the destination but loses the source-retirement metadata response, it reports
`destination published; source removal unknown — re-read both paths`.
An S3 journal publishes metadata and chunks together in one request. Force a
journal refresh before re-reading paths after an unknown outcome.

Every mutating tool names its operation and retry class:

- **NON-IDEMPOTENT:** append, prepend, search-replace, after-heading insertion,
  canvas node add and canvas edge add. A blind retry can duplicate the change:
  **re-read the note and apply the remaining change manually rather than
  retrying blindly**.
- **IDEMPOTENT FOR THE SAME INPUT:** overwrite, delete, move, copy, template
  create, note create, attachment upload, properties set/delete, tags add/remove
  and canvas node/edge remove. **Re-read to see whether it applied; retrying the
  same request is safe**. Create repeats may report “already exists”; tags add
  deduplicates. This describes the same request, including paths, values and IDs.

The journal peer commits a pending outbound key and payload digest locally
before sending, then confirms it after exact remote read-back. Replay/refresh
recognizes its own matching publications independently of `last_sent_key`,
applies and marks them processed, and repairs last-sent to the newest confirmed
outbound journal. Reconciliation is idempotent. An unpublished pending intent
is removed when its key is absent; a colliding key with different bytes is not
claimed. Removing an obsolete old key after reconciliation causes no refusal;
a missing newest confirmed journal still triggers wipe detection. Resets retain
local note revisions and chunks.

The preservation invariant applies to **peer-initiated mutations refused before
application**. It does not promise unchanged state after an unknown outcome.
Incoming replay reflects remote truth incrementally, like a device: journals
already applied remain applied if a later download fails. Retry refresh to
continue replay.

After confirmed remote publication/read-back and local metadata commit, journal
processed/last-sent tracking retries at most three times. Exhaustion logs a
warning and still returns success. Replay idempotently repairs an unmarked
journal and repairs last-sent tracking on the next refresh, without another write.
