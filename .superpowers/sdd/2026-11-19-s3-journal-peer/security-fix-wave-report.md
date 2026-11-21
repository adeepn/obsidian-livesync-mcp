# Security fix wave — 2026-11-21

Status: COMPLETE — four Important findings and one Minor finding fixed on
`plan-b-s3-journal`. Offline checks only; no shared hosts, cloud accounts or
live vaults were accessed.

## Premises

The replay path read complete S3 bodies and inflated DEFLATE streams without
bounds; JSONL splitting allocated all lines before parsing. Revision generations
were converted to Python integers without checking SQLite's signed range.
Publication used separate existence and upload operations. Refresh enumerated
the whole bucket without aggregate budgets and throttled successful passes only.
Local state directories inherited the process umask.

Source boundaries: `Boto3ObjectStorage.get`, `inflate_journal`, `parse_journal`,
`parse_rev`, `JournalBackend.refresh`, `_list_journals`, `_replay_one`, `_publish`,
and `JournalStore.__init__`/`apply_journal`.

## Execution trace and resulting behavior

1. **Bounded replay and revision generations — `7a43d46`.** Download checks
   ContentLength before reading and streams bounded blocks with a one-byte
   overflow probe. The default compressed limit is 512 MiB. Inflation uses
   `decompressobj.decompress(max_length=limit + 1)` without an unbounded flush;
   overflow/unconsumed tail raises `ValueError("journal inflated output exceeds limit")`.
   The inflated limit is 1 GiB. Parsing scans bounded line offsets instead of
   splitting the whole buffer; defaults are 100,000 records and 64 MiB per line.
   JSON nesting is capped at 512 levels, ignoring quoted brackets; decoder
   RecursionError is normalized to ValueError with file/line context. Revision
   generations outside `0..2**63-1` fail before SQLite binding. Replay errors name
   the file and reason class only and leave that file unprocessed.
2. **Atomic create-only publication — `cc454c9`.** The protocol adds
   `put_if_absent` returning `created`, `exists` or `unsupported`, with no delete.
   Boto3 uses `IfNoneMatch="*"`; 412/PreconditionFailed means occupied, supported
   not-implemented/unsupported codes and HTTP 501 mean unsupported, and other
   failures retain typed storage errors. Unsupported capability logs once per
   adapter. The fake performs check/set under one lock. Publication regenerates
   occupied keys with at most ten attempts and retains read-back verification.
   Unsupported providers take the documented B-10 exists-precheck + put path.
3. **Refresh budgets and failure backoff — `da04e3d`.** Defaults are 1,000,000
   examined keys, 4 GiB downloaded bytes including control reads, and 300 seconds.
   Listing requests are bounded; partial key sweeps resume from their last listed
   key and restart from the beginning at sweep completion. Files within each
   window are sorted numerically. Byte/time stops retain completed files and
   retry the unfinished window using the processed ledger. Pagination rejects
   non-advancing last keys and repeated S3 continuation tokens. Every attempted
   refresh, including failures and budget stops, arms the throttle; force=True
   bypasses it. Existing tests expecting immediate unforced retries were updated
   to the requested backoff contract.
4. **Private local state — `e1e2208`.** State/chunk/shard directories are created
   with 0700. SQLite is pre-created with 0600 before SQLite opens it; SQLite
   sidecars inherit its restrictive mode. Staged chunk files use tempfile's 0600
   and retain it through atomic replacement. Opening state tightens permissive
   existing directories and logs each actual correction once. Directory walks
   do not chmod symlinked directory targets.

Limits are documented module constants and Python keyword/constructor options;
no new environment variables were introduced. README documents overrides,
partial refresh behavior, private state modes and the B-10 compatibility race.

## Verification and durable artifacts

- Baseline supplied by reviewer: **844 passed, 7 skipped, 3 subtests passed**.
- Final full suite: **871 passed, 7 skipped, 3 subtests passed**, exit 0.
  Command: `venv/bin/python -m pytest -q`.
  Artifact: [security-fix-wave-pytest.txt](security-fix-wave-pytest.txt).
- Python bytecode compilation: passed for changed implementation/test modules.
  Artifact: [security-fix-wave-build.json](security-fix-wave-build.json).
- Initial failing regression run:
  [security-fix-wave-red.txt](security-fix-wave-red.txt).
- Intermediate focused run: 284 passed, 2 skipped (before additional probes).
  [security-fix-wave-focused.txt](security-fix-wave-focused.txt).
- `git diff --check` and staged diff checks passed; implementation diff read.

Regressions cover scaled compressed bombs, exact inflate boundary, oversized
lines and record counts, nested JSON and explicit decoder RecursionError,
SQLite generation overflow with repeatable errors and convergence after hostile
file removal, safe per-file error messages, atomic competing-creator preservation,
unsupported fallback, S3 conditional result/error mapping, key/byte/time partial
stops and continuation, non-progressing pagination, failed-refresh backoff and
POSIX directory/file permissions.

## Conclusions and rejected paths

All requested fixes have offline regression coverage, with the complete suite
remaining green. No remote objects can be deleted through the storage protocol.
The conditional path preserves both the rival object and the regenerated upload.

Unbounded `flush`, full-buffer JSONL splitting, unconditional overwrites after a
successful precheck, and success-only throttling were rejected because they
retain the reviewed failure mechanisms. A permanent high-water cursor was also
rejected: bounded sweeps must restart so late lexicographically smaller keys
remain discoverable (B-8).

Practical limits: the time budget is cooperative between synchronous operations,
not a preemption of an in-flight request, parser or SQLite transaction. The
listing continuation is process-local; restarts repeat listing from the start
while the durable processed ledger preserves applied work. Numeric ordering is
within bounded windows, rather than across an arbitrarily large complete bucket.
Budget settings must accommodate at least one permitted journal and control
reads. Unsupported S3 providers retain the explicitly allowed B-10 publication
race. Env-gated live integration checks remain skipped; no new live validation
is claimed.
