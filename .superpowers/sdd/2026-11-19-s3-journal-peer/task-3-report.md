# Task 3 Report: journal local store (revision trees + chunk store)

Commit: `feat: journal local store with revision trees` (branch `plan-b-s3-journal`)
Files: `journal_store.py` (new, ~370 lines), `tests/test_journal_store.py` (new, 40 tests)

## Implemented

All Task 3 interfaces, consumed verbatim by Tasks 5/6/7:

- `pick_winner(leaves: list[dict]) -> dict` — pure. `parse_rev` splits `"<int>-<hash>"`; max generation wins, tie → lexicographically **largest** hash, empty list → `ValueError`, malformed `_rev` → `ValueError`.
- `class JournalStore(state_dir: Path, path_identity: str)`:
  - `apply_journal(name, chunks: dict[str, str], docs: list[dict]) -> None` — idempotent (early-return on `processed(name)` + `INSERT OR IGNORE` + chunk-file existence dedup, so re-applying changes nothing observable); chunk data written verbatim (UTF-8 bytes) to content-addressed files `state_dir/chunks/<hh>/<rest>` (hex shard = first 2 chars of the id's hash part; ids shorter than 3 hash chars go to `chunks/_short/`; write is tmp-file + `os.replace`, atomic); docs inserted as rev-tree rows only if `(doc_id, gen, rev_hash)` absent; docs + processed marker in ONE sqlite transaction (bad file → nothing inserted, name not marked — tested).
  - `processed` / `mark_processed` / `files_processed_count`; `last_sent_key` property (default `""`); `epoch` property (default `""`) — setting a different epoch calls `reset()` (wipes docs/processed/meta rows AND all chunk files + shard dirs, preserves the path-identity config row, logs at WARNING with the removed-file count); setting the same epoch is a no-op.
  - `winner(doc_id)` (None when unknown), `leaves(doc_id)` (all stored leaves ascending `(gen, hash)` — see semantics below), `note_ids()` (sorted; `is_note_document` on the winner, so tombstone winners are excluded), `read_note(doc_id) -> (winner_doc, text, data, is_text) | None`.
  - `read_note` semantics mirror `storage_couchdb._content_from_doc`: None for unknown/non-note/tombstoned; legacy `notes` body via `legacy_body` (data=None, is_text=True); `plain` = children-order concat via `assemble_text` (data=None); `newnote` = per-chunk base64 via `assemble_binary`, `is_text_doc` decides text-ness, UTF-8 decode failure degrades to binary; empty-children branch matches the CouchDB backend; chunk resolution order = eden inline (`extract_inline_chunks`) → chunk store file → full-JSON chunk record in docs table; E2EE refusals raise `ValueError` with "uses end-to-end encryption" text (winner `e_`, legacy `%`-prefixed data, encrypted eden leaf, `e_` chunk doc); corrupt bodies raise `ValueError` carrying the path only (chunk ids stay out of user-facing messages, audits C4/C7).
  - `iterate_notes(extension=".md")` — generator of `(winner_doc, text_or_None)`, extension-suffixed filter (case-sensitive like upstream `isPlainText`), per-note isolation: unreadable note yields `text=None`, never raises.
  - `close()`.
- SQLite v1 schema exactly as specified: `docs(doc_id TEXT, gen INTEGER, rev_hash TEXT, doc_json TEXT, PRIMARY KEY(doc_id, gen, rev_hash))`, `processed(name TEXT PRIMARY KEY)`, `meta(key TEXT PRIMARY KEY, value TEXT)`; the composite PK indexes `doc_id` lookups. DB file `state_dir/journal.sqlite3`, `PRAGMA journal_mode=WAL` (persistent — tested by reopening), `PRAGMA synchronous=NORMAL`.
- Path identity: validated against `{PATH_IDENTITY_LEGACY, PATH_IDENTITY_NFC}` at construction (unknown value → `ValueError`); stored in `meta` on first open; reopen with a different identity → `ValueError` naming the state dir and both values, stored value untouched (tested). The store never derives or rewrites ids — a journal-delivered NFD doc id is preserved byte-for-byte in NFC mode (tested).

## Revision-tree semantics ([DECISION])

Journal records carry CouchDB `_rev` but no `_revisions` parentage, so ancestry is unknowable at this layer. Every stored revision is kept as an independent leaf — the same thing upstream's `new_edits=false` PouchDB inserts produce — and `pick_winner` decides the winner. Losing siblings stay queryable via `leaves()`. Tasks 6/7 needing conflict signals can compute "distinct leaves at the winner's generation" from `leaves()`; the winner is identical under any pruning because max is max.

## TDD evidence

1. RED: wrote `tests/test_journal_store.py` first (40 tests); run → `ModuleNotFoundError: No module named 'journal_store'`.
2. GREEN: implemented `journal_store.py` → 39/40; the single failure (`test_state_persists_across_reopen`) was a **test-ordering bug**: it applied a journal before ever setting the epoch, and the first epoch set (different from the `""` default) legitimately resets per spec. Fixed the test to the realistic Task 5 order (epoch from sync params → replay); no implementation change.
3. `tests/test_journal_store.py`: 40 passed.
4. Full suite: **642 passed, 1 skipped, 3 subtests** (baseline 602+1 kept green, +40).

Brief's named cases, all covered: winner table (3>2, tie→larger hash, empty→error); merge two journals with sibling leaf → both leaves stored, single winner, `note_ids` without dups; tombstone winner hides note (leaf kept); idempotent `apply_journal`; epoch change wipes tables AND chunk files; identity mismatch on reopen → `ValueError`; unicode doc id untouched in NFC mode; corrupt note (missing chunk) → `text=None` in iteration while others read fine.

## Files changed

- `journal_store.py` (new)
- `tests/test_journal_store.py` (new)

## Schema sketch

```
journal.sqlite3 (WAL, synchronous=NORMAL)
  docs(doc_id, gen, rev_hash, doc_json, PK(doc_id, gen, rev_hash))  -- every rev ever seen
  processed(name PK)                                                 -- applied journal ledger
  meta(key PK, value)                                                -- path_identity, epoch, last_sent_key
chunks/<hh>/<rest>        -- chunk data, UTF-8 bytes, dedup by existence
```

## Self-review

- Constraint check: stdlib only (`json/sqlite3/re/os/re/pathlib/logging`); no I/O outside `state_dir` (DB, WAL/SHM sidecars and chunk tree all live there).
- Chunk files are read with `read_bytes().decode("utf-8")`, never `read_text()` — text mode's universal-newline translation would silently corrupt `\r\n` chunk data.
- Chunk writes are tmp+`os.replace`, so a crash can't leave a truncated file that existence-dedup would then immortalize; a crash between file write and DB commit only leaves content-addressed orphans (harmless, dedup'd).
- Error-message hygiene mirrors audits C4/C7: user-facing corruption/encryption messages carry the note path, never chunk ids.
- Full-JSON chunk records (Task 2: `h:` docs with `_rev` + `data` land in `docs`) are honored as a chunk-data source at read time, so notes whose chunks arrived in JSON form still assemble (tested).

## Concerns

1. **Plan-text conflict, adjudicated to the dispatcher**: the plan's Step-1 text said pick_winner "пусто → None", while the dispatching interface (twice, explicitly) said empty → `ValueError`. Implemented `ValueError`; the `None` semantics live where they belong — `winner()` returns `None` for unknown ids. Test pins the `ValueError`.
2. First-ever epoch set on a fresh store (from `""` default to a real epoch) triggers a logged `reset()` per the literal "setting a DIFFERENT epoch calls reset()" — a no-op wipe plus one WARNING. Task 5's flow (epoch known before replay) makes this a once-per-state-dir event; if the log noise bothers anyone, skipping the reset when the store is empty is a safe relaxation.
3. `leaves()` is a superset of a true rev-tree's leaves (all revisions, not just non-superseded branches) because journal records carry no parentage — documented in the module docstring and above; conflict-detecting consumers must compare at the winner's generation rather than `len(leaves) > 1`.
4. `iterate_notes` re-fetches the winner per id after `note_ids()` already computed it (two indexed queries per note). Clear and cheap at vault scale; batch-fetching would be a pointless optimization now.

## Fix round 1 — 2026-11-19

### Inherited vs implemented

- Inherited branch `plan-b-s3-journal` with only `tests/test_journal_store.py` dirty. The supplied regression tests reproduced 6 failures / 38 passes locally, rather than the reported 5 / 39. One inherited assertion mistakenly matched JOURNAL_1 while applying JOURNAL_2; corrected that assertion so rollback coverage actually executes.
- Implemented strict `^h:[A-Za-z0-9+_-]{1,64}$` validation using fullmatch at shared read/write path construction, plus resolved-path containment against the pinned chunks root. Invalid apply IDs are skipped with warning; invalid children (including inline references) raise note-scoped corruption and yield text=None through iteration.
- Replaced shared temporary filenames with uniquely allocated staging files. Stage all new chunks, commit SQLite documents and processed marker, then publish through atomic os.replace. A pre-commit exception rolls back SQLite and removes all staging, preserving prior published chunks.
- Corrected pick_winner([]) to return None, including its return annotation and documentation. This supersedes the original report's empty-list contract and concern 1.
- Retained inherited regression tests; added same-journal duplicate replay with identical input, synchronized initial processed checks, invalid-ID length/charset/inline cases, and symlink-shard containment coverage. The inherited different-journal/shared-chunk race remains covered.

### Verification

- Full command: `./venv/bin/python -m pytest tests/ -q`.
- Result: **652 passed, 1 skipped, 3 subtests passed in 7.12s**.
- Durable output: `docs/test-artifacts/s3-journal/task-3-fix-round-1.txt`.
- Read final code/test diff; `git diff --check` passed.

### Concerns / [DEFER]

SQLite and filesystem publication are not one crash-atomic resource. A crash or rename I/O error after SQLite commit can leave committed metadata/processed marker with missing chunks; a local-state rebuild/replay is required. This round follows the requested commit-before-publication ordering and guarantees cleanup/no new publication for failures before commit. Protection against a hostile local actor changing symlinks between validation and access would require directory-descriptor-based operations; local state is assumed trusted.

## Fix round 2 — 2026-11-20

- [DECISION] Commit idempotent document inserts without the processed marker, publish all staged chunks, then call `mark_processed` in its own transaction. A publication failure leaves the journal replayable; the next apply preserves existing documents/chunks and publishes missing chunks. This supersedes round 1's rebuild requirement for publication failures.
- Added a regression that fails the second `os.replace`, checks committed documents, an absent processed marker, partial publication and staging cleanup, then replays successfully with no duplicate revisions and complete note content.
- RED: the new regression failed against round 1 because `processed(JOURNAL_1)` was already true.
- GREEN: `./venv/bin/python -m pytest tests/ -q` → **653 passed, 1 skipped, 3 subtests passed in 7.11s**.
- Durable output: `docs/test-artifacts/s3-journal/task-3-fix-round-2.txt`.
- Reviewed the code/test diff; `git diff --check` passed. Scope is limited to the processed-marker ordering and its regression.
