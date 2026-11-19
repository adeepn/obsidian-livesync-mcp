# Task 7 report: JournalBackend append-only write path

Status: complete; one logical implementation commit, subject
`feat: journal backend append-only write path`.

## Files changed

- `storage_journal.py`: implement write/delete/rename/copy and shared publication,
  revision-chain, chunk-validation, identity, and precondition helpers; track
  numeric maximum across the complete refresh listing, including processed keys.
- `tests/test_storage_journal.py`: 41 additional parametrized cases; replace the
  obsolete Task-7 NotImplementedError assertion with pre-startup refusal checks.
- `docs/test-artifacts/s3-journal/task-7-{red,expanded-red,green,full-suite}.txt`:
  durable pytest evidence.
- This report (explicitly included despite `.superpowers` being ignored).

## Behavior

Namespace validation precedes all storage access. Text writes refuse binary
extensions. Existing/missing/deleted targets follow the CouchDB D1/D2/B12 rules;
only explicit create resurrects a soft deletion. Optional mtime remains supported
by the existing StorageBackend interface. Stale expected_rev raises
ConcurrentModification before any put, including when the note is absent.

Each publication carries generation+1 with a random 32-hex revision hash and
`_revisions: {start, ids}`. Supplied ancestor chains are preserved in full and
validated; absent imported chains retain the known parent hash without inventing
older ancestry. A fresh destination starts its own generation-1 lineage; an
existing destination extends its own lineage, never the source's chain.

Writes emit one deterministic content chunk, validate any existing compact-file
or JSON-leaf representation before reuse, reset eden on body replacement, and
preserve ctime. Deletes keep children/type/path/eden and publish a new soft-delete
revision. Copy and rename preserve body references, eden, ctime and unknown fields
without decoding or reassembling content. Case-only rename changes one metadata
revision; same-path rename is a no-op. Rename emits destination and source soft
deletion in the SAME journal, so RenamePartialError has no partial phase here
and remains unused; existing tool-layer exception mapping is unchanged.

Publication chooses max(wall-clock ms, greatest observed numeric prefix + 1),
uses an increasing counter, and reserves the chosen prefix even on failure.
After put, get/inflate/parse must match both the exact uncompressed payload and
our decoded records before apply_journal or last_sent_key is touched. apply_journal
already marks the file processed. Success means durable publication, not eventual
winner status. There are no object deletes or repairs of shared chunks.

## TDD and verification evidence

1. Before production implementation, added the 29 brief-oriented cases.
   `./venv/bin/python -m pytest tests/test_storage_journal.py -q`:
   **29 failed, 75 passed**. All failures were missing Task-7 implementations.
   Artifact: `docs/test-artifacts/s3-journal/task-7-red.txt`.
2. Implemented the write path. The 29 new cases passed; the obsolete lifecycle
   test still expected NotImplementedError. Replaced it with StorageError before
   startup, as the implemented API now requires initialization.
3. Added edge coverage for transport errors, identical chunk reuse, eden reset,
   binary reference moves, tombstoned destinations/sources, same-identity copy,
   NFC/NFD identity and malformed ancestry. These additional verification tests
   were added after the initial implementation. To confirm regression sensitivity,
   temporarily substituted HEAD's original storage_journal.py, ran the expanded
   tests, then restored the implementation in a finally block:
   **42 failed, 74 passed** (41 added cases plus the revised lifecycle assertion).
   Artifact: `docs/test-artifacts/s3-journal/task-7-expanded-red.txt`.
4. Restored implementation, focused suite: **116 passed**.
   Artifact: `docs/test-artifacts/s3-journal/task-7-green.txt`.
5. Final full suite after source cleanup: `./venv/bin/python -m pytest -q`:
   **806 passed, 3 skipped, 3 subtests passed in 7.72s**.
   Artifact: `docs/test-artifacts/s3-journal/task-7-full-suite.txt`.
   Baseline supplied by dispatcher: 765 passed; net increase: 41 tests.
6. Read the source/test diff. Removed pytest-generated trailing whitespace
   from the RED log artifacts; `git diff --cached --check` is clean.

The fresh-peer test writes three notes through one backend, then starts and
refreshes a second backend with an independent state_dir and verifies content
and metadata equality. Other tests check read-back corruption/missing/foreign/
extra records without store changes, one-file rename with eden-only references,
full ancestor-chain preservation, stale preconditions without put, processed-key
name guards, same-ms writes/clock rollback, and a competing device sibling with
unique note ids. Tests use the real codec and SQLite/chunk store against
FakeObjectStorage; no network or production resources were used.

## Self-review

### Premises

- `storage_journal.py:395-399` inspects every refresh key before processed filtering.
- `storage_journal.py:685-728` checks expected revisions and creates ancestry.
- `storage_journal.py:731-758` validates shared chunks without mutation.
- `storage_journal.py:760-795` puts, reads back, decodes and checks before applying.
- `storage_journal.py:876-907` publishes both rename revisions in one file.
- `journal_store.py:184-225` stages chunks, commits metadata, publishes chunks,
  then marks processed; replay is retryable after local publication failure.

### Execution trace

1. Validate both paths (for move/copy), resolve vault identity, read local winners.
2. Check preconditions and create/overwrite rules before any put.
3. Construct revisions and validate any shared content chunk.
4. Serialize/deflate and allocate a greater numeric filename.
5. Put one journal, immediately get it, inflate/parse and compare exactly.
6. Apply verified records, mark processed through apply_journal, record last_sent_key.

### Conclusions

A stale revision cannot issue a put. Failed verification cannot modify local
metadata/chunks/checkpoint. A verified rename publishes both records together.
Copy preserves eden-only and binary bodies. Remote siblings remain available and
winner selection remains the store's deterministic policy. No blocking findings
remain from self-review.

### Rejected paths

- Separate destination/source journals: violates the required single-file rename.
- Reassembling copy/rename content: risks binary and eden data loss.
- Applying before read-back or accepting decoded subsets: could acknowledge a
  corrupt or substituted publication.
- Guarding names with only unprocessed keys: misses already replayed future keys.
- Refusing writes merely because len(store.leaves) > 1: the current store retains
  historical revisions as independent records, so that is not a conflict test.

## Concerns and deferred scope

- [GOTCHA] JournalStore documentation claims records lack `_revisions`; the design
  spec at `docs/superpowers/specs/2026-11-17-universal-livesync-mcp-design.md:110-121`
  and Task-7 brief require the complete chain. The write path follows that
  requirement. Existing local store winner/leaf behavior is unchanged; it retains
  historical rows rather than pruning ancestors. No real upstream journal fixture
  was available in this worktree; tests supply explicit chains.
- [GOTCHA] Mutation serialization currently relies on synchronous object/store
  calls without intervening awaits on one event loop. Thread offloading or async
  object access requires an explicit shared mutation lock before changing that.
- [DEFER] Live S3/device interoperability was not exercised; this task verifies
  the peer roundtrip using the protocol fake and real local stores/codecs.
- [GOTCHA] Chunk dedup validation uses JournalStore._chunk_path because the current
  store exposes no public chunk lookup API. The private helper retains its path
  containment checks; no store API changes were made outside the task's scope.
- [GOTCHA] Remote publication and local apply cannot be a distributed transaction.
  An apply/checkpoint error after successful verification returns StorageError;
  replay can reconcile the durable journal. A failed read-back also leaves its
  remote object in place; the write path never deletes objects.

## Fix round 1

Status: implemented both review requests under controller ruling B-10.

- [DECISION] Replace the instance-local counter in backend publications with
  `<ms>-<secrets.token_hex(8)>-docs.jsonl.gz`. Preserve the numeric timestamp
  floor and reserve it on failed attempts. Check each candidate with `exists`,
  regenerate occupied suffixes, and raise StorageError after ten collisions
  without issuing a put. This follows B-10's random allocation and precheck;
  the existence check is not an atomic conditional put.
- Extend `journal_filename` with a keyword-only `suffix` argument. Keep the
  older counter form for compatibility, documenting that it does not ensure
  cross-writer uniqueness. Existing numeric-prefix ordering already accepts
  arbitrary suffixes and requires no parsing changes.
- Add the reviewer regression with two independently initialized backends,
  shared FakeObjectStorage, the same fixed clock, and writes scheduled with
  `asyncio.gather`. Verify distinct keys, both original objects preserved, and
  both acknowledged bodies and metadata replayed by a fresh third backend.
- Add occupied-candidate regeneration and retry-exhaustion coverage. Existing
  remote objects remain byte-identical; exhaustion produces zero puts.
- Add local apply-failure injection before apply, before chunk replacement,
  and immediately after chunk replacement. Each failure returns StorageError
  after verified publication, leaves the remote object intact and its journal
  unprocessed, and preserves the prior last-sent checkpoint. A subsequent
  `refresh(force=True)` recovers body, metadata winner/leaves, and the processed
  checkpoint. Repeated refresh remains idempotent. Existing replay behavior
  passes these cases without a store implementation change.

Verification artifacts:

- `docs/test-artifacts/s3-journal/task-7-fix-round-1-red.txt`: the new focused
  tests against the original implementation: **4 failed, 3 passed**. Failures
  demonstrate cross-writer overwrite, absent allocation retry/exhaustion,
  and the missing filename suffix API. All three recovery cases already pass.
- `docs/test-artifacts/s3-journal/task-7-fix-round-1-full-suite.txt`:
  `./venv/bin/python -m pytest tests/ -q` exits zero with **813 passed,
  3 skipped, 3 subtests passed in 8.87s**, seven cases above the 806 baseline.
  No warnings appear in the full-suite output. Captured WARNING-level log
  records in the expected RED failures are the existing startup state-reset
  diagnostic from JournalStore, not pytest warnings or an additional failure.

Read the source/test diff before committing. No network or production resources
were used.
