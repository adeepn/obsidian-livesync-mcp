# Task 5b execution report

## Part 0 — P-17

Selection refusals now have a dedicated SelectionRefused carrying path and rule.
WriterUnsupported and MutationNotApplied include tool argument paths; the latter
states that the note is unchanged and preserves the sanitized cause. Unmapped
StorageError types preserve their sanitized diagnostic instead of alleging an
unavailable vault.

Base-raise audit: object_storage.py and journal_store.py use constant diagnostics;
server.py has empty validation errors; CouchDB and journal identity wrappers only
forward validate_document_identity's constant error. Other backend raises contain
constant diagnostics or vault paths/journal names (redacted at the tool boundary).
The journal publication parser was the exception: it forwarded arbitrary codec
ValueError text. It now emits a constant preflight diagnostic. URL, revision and
credential-assignment redaction adds defense in depth for future exceptions.

RED: /tmp/parity-p17-red.log (12 failures for lost detail/path).
Focused GREEN: /tmp/parity-p17-focused.log (93 passed).

The first full run found 33 assertions of the previous output contract. Updated
writer tests to require paths, and transport-redaction tests to inject a URL
containing the private marker rather than expecting every safe diagnostic to be
suppressed. An additional RED probe found that unknown subclasses of mapped
exceptions still inherited generic messages; exact-class dispatch now leaves
those subclasses on the sanitized-detail fallback (31 focused tests pass).

Final Part 0 suite: **3614 passed, 7 skipped**, 89.94 s.
Durable log: task-5b-p17-suite.log beside this report. Diff reviewed; diff check clean.

## Part 1 — assessment and startup

Premises (pinned sources):
- C:common/models/tweak.definition.ts:24-67 declares 21 compared keys, seven
  known defaults, three lossy keys with explicit order. Recommended keys are
  snapshotted but do not become comparison rules (:113-150).
- C:common/models/tweak.compatibility.ts:47-65,148-191 distinguishes missing,
  unadvertised and semantic equality; ID modes are equal without keyed doc IDs.
  Transitions preserve ID configuration and classify reconstruction (:69-145).
- C:pouchdb/LiveSyncDBFunctions.ts:19-24,79-118 computes effective metadata
  encryption, checks ID and normalization before !disableCheckingConfigMismatch.
- CouchDB reads/writes the same PREFERRED entry at the local milestone:
  C:common/models/db.const.ts:4; C:pouchdb/LiveSyncDBFunctions.ts:177-203;
  C:replication/couchdb/LiveSyncReplicator.ts:2304-2305,2358-2372. There is no
  separate CouchDB PREFERRED store. Journal uses the shared check
  (C:replication/journal/LiveSyncJournalReplicator.ts:209-249,542-553).
- P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:66-81,120-151
  selects greater positive numeric tweakModified, REMOTE for ties/two missing;
  missing autoAcceptCompatibleTweak defaults true. Booleans/strings are not numbers.

Execution trace:
1. Resolve PREFERRED or the existing first-readable-device fallback. Preserve
   old unreadable-map guards. Normalize both comparison inputs exactly once.
2. Assess the actual plaintext peer metadata preference and every advertised
   device against the baseline. Gather the complete differing-key list; check
   ID and normalization before the local mismatch gate. Never print values.
3. Refuse semantic/identity mismatches. For fully advertised lossy-only changes,
   select the upstream timestamp winner; preserve ID configuration and omitted
   target recommendations during adoption. All unsupported crypto modes still
   refuse even with checking disabled.
4. Build the existing splitter/identity/size policy from the chosen raw settings;
   raw splitter inputs avoid a second migration. Log lossy winners and safe keys.
   backend_info adds tweak_assessment {status, differing_keys}.

Local gate and auto-accept policy are exposed by strict boolean configuration
LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH=false and
LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK=true. They are not fabricated from remote
milestone entries: upstream local policy flags are outside tweak_values.

Source/brief boundaries:
- The brief/spec's phrase “gate enabled” is ambiguous: the actual condition is
  !disableCheckingConfigMismatch. Implemented the code's condition and clarified
  the spec. Identity pre-gate checks remain unconditional.
- Upstream CURRENT auto-accept publishes PREFERRED (P:317-340), not just a local
  setting change. The explicit brief instead requests local winner application
  and continuation without silently fixing devices. Implemented that scope:
  startup never publishes remote settings. A CURRENT winner needs explicit
  remote alignment before other devices can adopt it. README/spec disclose it.
- assessTweakCompatibility itself does not normalize. The brief requires Task 3c
  normalization first on the peer path. Missing raw splitter versions therefore
  differ from the upstream raw known v3 default after migration (legacy empty);
  both layers are differential-tested, rather than conflated.
- Upstream checks the connecting device, not all recorded devices. Checking all
  snapshots is the explicit extra peer startup requirement in this brief.

Rejected paths: template defaults for every missing key (would hide unadvertised
settings); Python false==0 (differs from JS strict equality); truthy timestamp
strings/booleans; propagating ID keys while adopting lossy knobs; re-normalizing
an already migrated unknown version; mutating PREFERRED/device entries at startup.

TDD artifacts: /tmp/parity-5b-{red,startup-red,effective-red,aggregate-red,adoption-red,config-red}.log.
Focused GREEN: /tmp/parity-5b-focused-final.log — 1061 passed.
Oracle: scripts/extract_tweak_vectors.mjs, committed gzip fixture
 tests/fixtures/tweak_compatibility_upstream.json.gz.
Reproduce:
  node --experimental-vm-modules scripts/extract_tweak_vectors.mjs /tmp/settings-parity-commonlib /tmp/settings-parity-plugin /tmp/tweak-oracle.json.gz
  ./venv/bin/python -m pytest tests/test_tweak_differential.py -q
429 seeded setting pairs each assessed raw and migrated (858 assessment checks),
486 upstream plugin auto-accept cases, complete rule inventory: 916 differential
tests. Six actual commonlib modules execute; no fake compatibility internals.
Every source is verified against git pins and hashed. Node's strip-only runtime
cannot erase a numeric enum; generic mechanical numeric-enum lowering preserves
its actual source members and reverse mappings. The plugin's real two policy
methods run verbatim; only Logger and persistence are external no-op boundaries.
Fixture source metadata records pins, Node version, seed, and SHA256s.

Full GREEN: **4616 passed, 7 skipped**, 89.18 s; durable task-5b-suite.log.
Python compileall and git diff --check passed. A fresh reviewer independently
checks the entire P-17 + 5b range and regenerates the oracle before final commit.

## Fresh independent review and fixes

Review artifact: task-5b-independent-review.md (the reviewed intermediate revision;
its six important findings were subsequently reproduced and fixed below). Reviewer
regenerated the original fixture byte-for-byte and independently executed **1,350
additional distinct ordinary randomized pairs** against pinned raw + normalized
assessment and plugin policy. Its two numeric boundary probes led to added vectors.
No production/hardware/network work was performed.

All six important findings reproduced in /tmp/parity-5b-review-red.log (22 failures):
- Concrete peer vs every device: a sparse PREFERRED must keep unadvertised semantics
  in each individual pair, but actual min/hash defaults (20/xxhash64) compared with
  an advertising device (80/sha1) are different. Added that final comparison.
- CouchDB enableCompression=true now compares against the actual false capability;
  this peer has no CouchDB transform adapter. Journal retains the advertised knob
  since its framing does not consult the CouchDB adapter.
- Original all-device size validation runs before flattening the winner; conflicting
  50/100 no-PREFERRED limits still refuse, independently of JSON key order. Explicit
  override still takes precedence.
- Lossy election considers every snapshot, including a newer snapshot equal to the
  original PREFERRED. Its later timestamp can beat an intermediate CURRENT winner.
- Readability now recognizes the complete comparison inventory, including sparse
  encryptInternalMetadata=false, maxAgeInEden and longLineThreshold snapshots.
- P-17 fallback sanitization now handles Authorization Bearer/Basic, quoted JSON/dict
  secret/revision fields, ID derivation keys and access/secret key assignments. The
  known safe idDerivationKey=<invalid configuration> diagnostic remains explanatory.

The numeric mismatch above 2^53 was graded minor, but the owner's explicit any-setting
parity requirement calls for JS Number precision. Added binary64 conversion in
assessment snapshots/equality and timestamp election, plus two real Node oracle
vectors preserving the original JSON literals. RED: /tmp/parity-5b-number-red.log.
The final fixture has **429 ordinary pairs (raw + migrated), 2 numeric boundary
pairs, and 486 plugin policy cases**: 918 differential pytest cases.
Final fixture independently re-generated again after the numeric addition:
SHA256 7bdbc2c14385e65d112bc981f0a92d32dd19f542a01f543ab23d03949d54a678.
Focused post-review GREEN: /tmp/parity-5b-review-focused2.log (1058 passed), plus
/tmp/parity-5b-redaction-focused.log (70 passed). The first post-review suite found
two expected-message failures for the safe invalid-ID-key placeholder; preserving
that exact known-safe diagnostic fixed them. Final full rerun follows below.

No review finding is deferred. Real production milestone capture and live device
acceptance remain Task 8; production-like startup values here are synthetic fixtures,
not a claim of a production compatibility check.

## Final status

Completed on settings-parity:
- d2ba26b fix: stop swallowing specific storage errors in tool messages
- 59ab2e0 fix: redact structured credentials from storage diagnostics
- a84a46c feat: device tweak compatibility assessment at startup

Final full suite: **4645 passed, 7 skipped**, 90.54 s.
Durable artifact: task-5b-final-suite.log. Final differential: **918 passed**,
task-5b-final-differential.log. Compileall and staged diff checks clean.
Re-executed the independent reviewer's unchanged 1,352-pair driver against the
final code (including both formerly failing numeric boundaries); final output
is preserved in task-5b-final-independent-differential.log.
Worktree clean after commits. Report and logs intentionally remain in the plan's
existing ignored .superpowers workspace, as requested; they were not deleted.
No deferred review findings. Remaining concern: local CURRENT adoption does not
publish remote PREFERRED; live acceptance remains Task 8, as documented above.

## Fix round 1

P-18 governs diagnostic construction and tool-boundary redaction. Part B assessment
semantics and oracle fixtures remain unchanged. No new oracle claims are made.

### Specific causes

Premise: exact-class messages dropped specific causes, including UnsupportedVault
and StorageUnavailable. The recursive subclass regression now requires the actual
safe diagnostic to survive every StorageError dispatch, including structured outcomes.
RED: `/tmp/parity-fix1-red.log` (8 lost-detail failures). The first full run found
one older test requiring suppression of any StorageUnavailable detail; its transport
payload now uses a private URL and verifies URL redaction while retaining the cause.
