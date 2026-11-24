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

### Uniform diagnostic redaction

Premises: mutation OutcomeUnknown bypassed `_storage_message`; WriteSkipped returned
raw JSON fields; writer-support settings could contain arbitrary objects. RED logs:
`/tmp/parity-hygiene-red.log`, `/tmp/parity-source-red.log`,
`/tmp/parity-nested-red.log`, `/tmp/parity-observability-red.log`.

Execution trace: exception dispatch sanitizes both contextual paths and causes;
structured outcomes sanitize string leaves before JSON encoding; backend_info
sanitizes writer/selection reasons. `sanitize_diagnostic` is the sole redaction
implementation: URL removal, percent/unicode/escaped-quote decoding, header/auth
redaction, credential assignments (including nested containers), entropy and AWS
ID redaction. Vault note contents are outside the diagnostic sanitizer.

The committed corpus covers plain, JSON, dict-repr, encoded and punctuation forms,
nested objects/arrays, mixed-case labels, long numeric passphrases, and bare auth.
The invariant injects every corpus payload through every recursively discovered
StorageError class and every registered reading/mutating tool (including action
branches). Separate real-backend and mutation uncertainty regressions exercise
the reviewer's exact reproductions. Focused hygiene: 2243 passed.

Rejected paths: suppressing every specific cause, sanitizing normal note bodies,
using redaction as permission to stringify arbitrary settings, changing assessment
normalization/selection semantics, regenerating accepted oracle fixtures.

Hygiene full suite: **6852 passed, 7 skipped, 3 subtests passed**, 96.44 s.
Durable artifact: `task-5b-fix1-hygiene-suite.log`. Diff check clean.

### Raise-site audit (P-18)

Audited every direct raise and exception factory in root storage modules, plus
indirect settings/selection/decode producers. The table uses post-fix source lines.
The reviewer's audit list was cross-checked against AST enumeration (146 sites).
"Safe" means the producer uses only approved structured data; paths still pass
through the sanitizer before tool exposure. It does not mean arbitrary paths
are trusted to be free of secret-shaped text.

Provenance of forwarding sites:
- CouchDB/journal `_validate_identity`: `validate_document_identity` raises a
  constant mismatch/collision reason, never document IDs, values or revisions.
- Startup settings wrappers: only fixed refusals, fixed schema key names,
  type/shape explanations and the splitter diagnostic described below.
- `resolve_content_splitter`: ContentSplitter `_number` reports fixed key names;
  UnsupportedWriterMode now reports hashAlg/chunkSplitterVersion/ID-version shapes;
  ID-key values are never stringified. No assessment comparison semantics changed.
- Journal `_translate_decode_error`: JournalStore note decoding emits paths and
  fixed corruption/encryption reasons; base64/eden errors are replaced by those
  fixed reasons. Replay failures instead use journal path + exception class.
- Journal publication parser: constant preflight failure; raw parser/revision text
  cannot reach StorageError. Revision-related store ValueErrors are contained by
  this preflight or the replay class-only wrapper.
- Selection: worker rule is an enum; arbitrary worker reasons are replaced by
  `Invalid device selection pattern`. EngineFailure uses literal markers and
  numeric exit status. No regex/settings values are interpolated.
- Budget constructors: literal budget name and numeric counts/limits only.
- WriteSkipped/RenameRefused: measured byte length or remote size after numeric
  comparison in check_file_size; validated numeric maximum. No arbitrary size
  object is formatted by a backend outcome.
- S3 failure factory no longer interpolates configured bucket values (including
  in its log). Object paths and literal operation names supply context.

Additional constructor/producer sites:

| Site | Interpolated data | Safe? | Change |
|---|---|---|---|
| storage_base.py:SelectionRefused | path, validated rule enum | yes; path redacted at boundary | retained |
| storage_base.py:WriteSkipped / RenameRefused | numeric sizes and maximum; path in JSON | yes; all JSON leaves redacted | uniform structured sanitation |
| storage_base.py:RenamePartialError | constant reason; paths held as fields | yes; contextual paths redacted | uniform outcome sanitation |
| storage_base.py:OutcomeUnknown | adapter detail built from paths/constants | yes in adapters; injected details also redacted | mutation retry path now sanitized |
| replay_limits.py:ReplayBudgetExceeded | literal budget name, numeric counts/limits | yes | retained |
| livesync_format.py:resolve_content_splitter | known splitter error | previously unsafe via splitter | fixed producers |
| content_splitter.py:unsupported list | setting values | previously unsafe | key + invalid type/shape, no str/repr |
| livesync_format.py:resolve_path_identity | pathNormalizationVersion value | previously unsafe | key + invalid type/shape, no repr |
| selection_engine.py:validate_response / EngineFailure | fixed marker / numeric status | yes; remote reason replaced | retained |
| Site | Interpolated data | Safe? | Change |
|---|---|---|---|
| `content_splitter.py:169` | fixed reason or invalid_setting key/shape list | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `content_splitter.py:273` | fixed reason or invalid_setting key/shape list | yes, structured; boundary redacted | replace raw hashAlg/splitter/ID version with key + shape |
| `device_selection.py:64` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `device_selection.py:72` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `device_selection.py:191` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `device_selection.py:193` | paths; validated rule/source-state marker | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_codec.py:180` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_codec.py:216` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_codec.py:219` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_codec.py:269` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_codec.py:292` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_store.py:216` | label | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `journal_store.py:241` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:224` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:230` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:234` | type(preferred).__name__ | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:249` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:278` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:288` | pathNormalizationVersion key and type/shape | yes, structured; boundary redacted | replace raw version repr with key + shape |
| `livesync_format.py:466` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:468` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:478` | known splitter diagnostic; key and shape only | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:480` | known splitter diagnostic; key and shape only | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:485` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:891` | fixed mismatch classification and compared key names | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:897` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `livesync_format.py:929` | fixed mismatch classification and compared key names | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:170` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:178` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:182` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:184` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:191` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:195` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:196` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:204` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:226` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:228` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:244` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:256` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:277` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:279` | literal operation; object path; transport class, never transport text | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `object_storage.py:291` | operation, key | yes, structured; boundary redacted | remove configured bucket value; retain object path |
| `server.py:381` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `server.py:407` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `server.py:644` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `server.py:669` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `server.py:706` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `server.py:711` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_base.py:67` | path; measured/compared numeric size and validated maximum | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:214` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:215` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:234` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:236` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:241` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:242` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:243` | code | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:249` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:262` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:278` | known identity/settings/decode diagnostic (see provenance below) | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:291` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:321` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:416` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:443` | known identity/settings/decode diagnostic (see provenance below) | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:530` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:536` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:643` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:646` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:676` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:695` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:711` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:737` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:757` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:759` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:782` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:789` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:808` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:832` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:852` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:859` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:873` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:907` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:914` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:929` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:938` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:954` | new_path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:980` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:1042` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:1044` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:1113` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:1125` | path; measured/compared numeric size and validated maximum | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:1147` | path, new_path, path, new_path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_couchdb.py:1160` | paths; validated rule/source-state marker | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:179` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:181` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:183` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:267` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:304` | known identity/settings/decode diagnostic (see provenance below) | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:320` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:359` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:383` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:414` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:424` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:430` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:482` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:486` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:502` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:506` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:514` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:615` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:641` | name | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:664` | name, type(exc).__name__ | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:671` | operation | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:747` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:870` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:894` | known identity/settings/decode diagnostic (see provenance below) | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:895` | known identity/settings/decode diagnostic (see provenance below) | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:916` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:939` | known identity/settings/decode diagnostic (see provenance below) | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:946` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:962` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:976` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1000` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1008` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1012` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1033` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1042` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1048` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1056` | literal budget name; numeric sizes/counts/limits | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1061` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1076` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1084` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1093` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1098` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1101` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1114` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1119` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1130` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1133` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1137` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1200` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1202` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1210` | constant diagnostic / empty outcome / fixed setting-key reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1254` | path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1262` | new_path | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1288` | safe writer/validated selection reason | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |
| `storage_journal.py:1299` | path; measured/compared numeric size and validated maximum | yes, structured; boundary redacted | retain; sanitize at diagnostic boundary |

Source fixes full suite: **6934 passed, 7 skipped, 3 subtests passed**, 95.72 s.
Durable artifact: `task-5b-fix1-sources-suite.log`. Focused producer + hygiene +
writer/identity tests: **2393 passed**. Additional RED proofs:
`/tmp/parity-bucket-red.log` and `/tmp/parity-base64-red.log`.
The S3 bucket configuration is no longer reflected; token redaction also covers
single-case runs containing '+' or '/', and decoding continues to a fixed point.
Both review reproductions pass. Diff and diff-check reviewed clean.

### Folded authorization and encoded userinfo follow-up

The extended corpus found two further concrete redaction gaps: folded Authorization
values and URL userinfo containing encoded whitespace, angle brackets or '@'.
RED proofs: `/tmp/parity-folded-auth-red.log`, `/tmp/parity-url-encoding-red.log`,
`/tmp/parity-url-userinfo-red.log`. The sanitizer now removes the authorization
remainder and complete userinfo-bearing URLs after decoding, including those forms.

A second RED (`/tmp/parity-auth-guidance-red.log`) showed why sanitizing only the
final OutcomeUnknown sentence was insufficient: aggressive header redaction could
also erase uncertainty and retry guidance. The wrapper now sanitizes contextual
paths and the cause before building that sentence, then sanitizes the complete
outcome as the final defense. Direct `_storage_message` follows the same ordering.
The source still constructs causes only from approved structured fields.
Focused hygiene + specific-message regressions: **2510 passed**.

Authorization/encoded-userinfo full suite: **7125 passed, 7 skipped,
3 subtests passed**, 90.79 s. Durable artifact: `task-5b-fix1-auth-suite.log`.
Diff reviewed; diff check clean. This follow-up is kept separate from the
vault_stats/documentation correction as a distinct security change.

### Prefixed credential labels follow-up

The adversarial corpus also covers aws_secret_access_key, aws_session_token,
db_password and camel-case dbPassword in plain, mixed-case JSON, dict-repr and
percent-encoded forms. `/tmp/parity-prefix-red.log` records 24 leaking cases.
The shared label recognizer now accepts a field-name prefix before a known
credential label; no second sanitizer or backend-specific rule was added.
Focused hygiene + specific-message regressions: **2966 passed**.

Prefixed-label full suite: **7581 passed, 7 skipped, 3 subtests passed**, 95.46 s.
Durable artifact: `task-5b-fix1-prefix-suite.log`. Diff and diff check clean.
### vault_stats and device publication documentation

RED: `/tmp/parity-stats-red.log` — missing tweak_assessment in tool output.
vault_stats now explicitly projects only status and differing_keys and sanitizes
that diagnostic structure; it cannot forward additional assessment values. Three
focused tests cover the projection and both actual backends using fake transports.

The spec now records both auto-accept publication paths: CURRENT updates remote
PREFERRED; REMOTE saves local settings, reinitializes managers when no rebuild is
required, and also updates PREFERRED (pinned plugin ModuleResolveMismatchedTweaks.ts:
229-231,333-350). Successful compatibility checking publishes the device snapshot
on milestone updates, including periodic last-connected updates, and initializes
missing PREFERRED (pinned commonlib LiveSyncDBFunctions.ts:153-170). README/spec
state the peer's explicit invisible-writer policy and the public Python API's
migration of both inputs before assessment.

Verification artifact: `task-5b-fix1-verification.json` records successful compilation
of all eight modified Python modules and AST identity against a84a46c for all eight
tweak assessment/startup functions. Accepted differential fixtures and assessment
semantics are unchanged; no synthetic replacement oracle was introduced (P-7).

Final full suite: **7584 passed, 7 skipped, 3 subtests passed**, 96.63 s.
Durable artifact: `task-5b-fix1-final-suite.log`. Verification JSON refreshed for
the final source tree, including SHA256 per changed Python module. The original
seven skips remain; no live storage or device operations were performed.

Conclusions: specific causes survive all StorageError classes; one sanitizer
covers exception outcomes and reason fields; setting values never enter writer
or path-normalization diagnostics; mutation uncertainty/retry guidance survives
redaction; vault_stats matches its documented assessment contract. The requested
fix-round changes are complete. Assessment semantics and fixtures remain intact.
Known outstanding concerns for this round: none. Final diff reviewed and
`git diff --check` clean. Each logical commit has a passing full-suite artifact.


## Fix round 2 — P-19 white-list construction

### Premises

The reviewer corpus contains 102 independent synthetic payloads. The reviewed
matrix contains 22,746 diagnostic evaluations and the echo matrix 1,836
caller-input evaluations. `/tmp/parity-rereview-report.md`, matrix, echo and
performance JSON were read before implementation. `red.log` reproduces the
schemeless leak; `generic-red.log` reproduces the generic-exception boundary;
`count-red.log` reproduces a remote string in CouchDB `doc_count`.

The previous boundary appended `str(error)` and re-filtered composed output.
That cannot establish provenance and loses legitimate filenames. The previous
regex also rescanned unbounded free text. Those paths have been removed.

### Execution trace

1. Producers attach a closed `DiagnosticCode`, `PublicDiagnostic`, typed setting
   issues, numeric budgets/invalid-byte counts, or explicitly sourced paths.
2. `DiagnosticPath.remote` is mandatory: caller paths escape controls only;
   metadata/body paths also normalize NFC. The wrapper obtains original caller
   arguments from its signature binding, never from backend error text.
3. Public exception `format()` methods consume these fields. Unknown/free-text
   reasons cannot add a new public string; legacy internal literals must match
   the closed inventory. Exact-class dispatch retains safe typed causes.
4. Tool boundaries retain outcome and retry guidance; ordinary unexpected
   exceptions become a class-only failure. There is no `detail` interpolation,
   recursive string-leaf sanitizer, or free-text tool-output sink.
5. Backend observability constrains enum/key fields, hides database/local config
   paths, fingerprints epoch/update_seq, validates `doc_count`, and counts
   unfamiliar journal metadata fields. Real replay restricts journal names.
6. Default logs retain fixed events/statuses/operations/classes. Response error
   fields, version values, invalid IDs, regex/stderr payloads and tracebacks are
   withheld. The explicit DEBUG sink withholds every nonempty free-text value.

### Route → fields used → free text allowed?

| Route | Fields used | Free text allowed? |
|---|---|---|
| `_storage_message/StorageError`, unknown subclasses | closed reason code, inventoried keys, cause class | no |
| `UnsupportedVault`, `StorageUnavailable`, `WriterUnsupported` | outcome prefix, typed reason/key/issue fields, cause class | no |
| `NoteNotFound`, `NoteExists`, `ConcurrentModification`, `UnresolvedConflicts`, `_TextRequiredError` | fixed class semantics plus typed reason/path fields where supplied | no |
| `MutationNotApplied` | unchanged guidance, original caller context, closed reason/class | no |
| `OutcomeUnknown` and all 19 retry variants | original caller context, source-removal flag, cause class, known operation, unchanged retry text | no |
| `WriteSkipped.as_dict` and wrapper | original caller path, byte size, numeric limit, fixed not-saved guidance | no |
| `RenameRefused` | caller source/target context, size, limit, fixed refusal guidance | no |
| `RenamePartialError` | caller source/target or explicitly sourced path fields, source-state enum, fixed recovery text | no |
| `SelectionRefused` | typed path, inventoried rule key, fixed exclusion guidance | no |
| `ReplayBudgetExceeded`, `_RefreshBudgetStop` | known budget name, numeric observation/limit/capacity, fixed env-variable guidance | no |
| Journal replay failure | validated journal name or unexpected-name length, cause class | no |
| Both writer-support checks | typed setting issues (key + shape) or fixed selection diagnostic | no |
| Both `backend_info` implementations | closed backend/state/path-identity enums, safe key/issue projections, validated counts, fingerprints | no |
| `vault_stats` assessment | closed assessment status and inventoried differing keys only | no |
| `vault_stats` local state | hidden config path, numeric size/threshold, fixed warning | no |
| Refresh/tool wrapper failures (all tool/action variants) | original caller path fields, fixed outcome, cause class | no |
| Missing note/template/canvas branches | original caller path with visible control escapes | no |
| Heading/node/edge and unknown mode/action echoes | caller's own escaped arguments, fixed syntax guidance | no |
| Create/update/move/copy/template/canvas successes | caller's own escaped paths/IDs, counts, known operation | no |
| Invalid plain-byte upload | original caller path, invalid-byte count, fixed conversion guidance | no |
| Default server logs | fixed event/operation, numeric status, cause class | no |
| Explicit `LIVESYNC_UNSAFE_DEBUG=1` at DEBUG | capped free-text input; output is empty or `[redacted]` | yes, input only; never retained |

### Conclusions and verification

The committed test corpus is `tests/fixtures/diagnostic_attack_corpus.json`.
Parametrized coverage includes every loaded StorageError subclass through every
tool/action, all 18 original echo routes, real backend setting/diagnostic
projections, real hostile journal replay, generic exceptions, logging, explicit
DEBUG opt-in, caller over-redaction regressions, and event-loop timing budgets.
The final suite and benchmark results are recorded in
`docs/test-artifacts/task-5b-fix2/verification.json` and `timings.json`.

`sanitize_diagnostic` slices at most 2048 characters and returns a fixed marker;
it has no regex, decoding loop or scan of the remaining input. Its time/space
bound is O(min(n, 2048)); `scripts/benchmark_diagnostics.py` records 30 runs each
for letters, dots, percent encodings, nested labels and a URL-at-end at 1 KB,
100 KB and 1 MB. The tests require <50 ms at every size.

The pinned upstream stores salt in sync parameters and passes it with the
passphrase to HKDF (`JournalSyncCore.ts:344,467–468,523–524`); this is protocol
metadata, not demonstrated standalone credential material. The conservative
public representation is a 16-hex SHA-256 epoch fingerprint. Internal epoch
comparison and replay semantics are unchanged.

Part B assessment/election function bodies are byte-for-byte unchanged, as
recorded by `parity-guards.json`. Existing differential fixtures are unchanged;
no synthetic substitute oracle was introduced (P-7). Existing tests changed
only where they required free-text causes, raw log payload/identifiers/config
paths, raw metadata names or salts. Outcome/retry, loss-path and Part B fixture
assertions were retained.

Independent review found extra raw observability/generic-exception/caller-echo
routes; they were closed, re-reviewed and reported `NO_NEW_FINDINGS`.

### Rejected paths and concerns

- Extending the blacklist cannot prove secrecy and reintroduces path loss.
- Applying last-line redaction to a composed message destroys note identity and
  retry context; that design was removed entirely.
- No raw-detail DEBUG escape hatch is provided: the opt-in sink intentionally
  withholds all nonempty unstructured input. Diagnostic detail is consequently
  less conversational; typed fields and class names are the supported interface.
- Caller paths and actual remote note paths are data and may themselves look
  like credentials; their preservation is intentional, not a leak-detector
  exception inferred from free text. Remote paths never become caller arguments.

Final strengthened suite: **8532 passed, 7 skipped, 3 subtests passed** in 117.56 s.
Baseline 7584 + 948 new tests; no baseline tests removed. Redactor maxima: 1 KB
0.000625 ms; 100 KB 0.000458 ms; 1 MB 0.000375 ms. Independent targeted review:
978 passed, final `NO_NEW_FINDINGS`. Build and diff checks passed.

Documentation mismatch found: old journal comments claimed a 16-hex suffix,
but the publisher calls `secrets.token_hex(16)` (32 hex characters). Comments
now match the actual publisher and the required diagnostic grammar; no
filename-generation behavior changed.


The follow-up test commit strengthens every remote-origin route to reject all
six-character secret windows after decoding/Unicode canonicalization, and
residual hex/token/JWT/PEM/base64-shaped runs. Applied retrospectively to the
reviewer's saved diagnostic outputs (excluding caller echoes), these assertions
reject 7905 of 22644 historical evaluations (`strengthened-red.json`); these
counts are synthetic marker/shape checks, not independent vulnerabilities.
Repeated percent and nested-label event-loop cases add six tests. This is
regression-test evidence, not a new upstream parity oracle.

Commits so far: `acf695a` (typed construction, bounded fallback, log hygiene)
and `0948222` (residual-secret assertions). README, CHANGELOG, P-18/P-19 spec
and this report document the final interface and its deliberate limitations.

Documentation pre-commit full suite: **8532 passed, 7 skipped, 3 subtests passed**
in 96.55 s (`docs-suite.log`). Final diff check passed. No unresolved blocker;
free-text DEBUG output is deliberately withheld wholesale.


## Fix round 3 — P-21 typed specifics and total outcome reporting

[DECISION] Retain typed HTTP status, operation and sanitized cause class through
CouchDB, S3 adapter and journal publication/readback uncertainty. Preserve safe
authorization/database/concurrency reasons and existing retry classifications.
Operations are GET/PUT/POST/DELETE/HEAD/LIST; no bodies, headers or object keys
are diagnostic inputs. Missing-object GET/HEAD remains ordinary absence.

Validate numeric diagnostic fields at construction and render: exact int,
bool excluded, 0 <= x < 2**63; malformed values become <invalid> without
coercion. An omitted optional HTTP status means no response. An explicitly
supplied malformed decoder capacity, including None, is <invalid>.
Environment replay limits, including seconds, are positive bounded integers.
Reasons/keys/budgets/algorithms retain closed inventories; cause names use
[A-Za-z_][A-Za-z0-9_]{0,63}; invalid names become <unexpected cause>.

[DECISION] Escape C0/C1, separators and bidi embeddings/overrides/isolates in
free-text paths. Pass zero-width U+200B/C/D and BOM U+FEFF unchanged because
they can belong to a note identity and do not reorder text. Structured JSON
paths preserve exact identity. Contain renderer failures with explicit
outcome-unknown/re-read guidance; full exception detail stays in logs.
Restore ordinary Python argument validation before starting execution.
The bounded debug redactor handles non-string inputs without coercion.

The committed attack corpus now contains all 121 reviewer payloads. Added
real CouchDB fake-transport status/operation coverage (11 statuses x four
operations), S3 connection/timeout/throttle/404 coverage, journal uncertainty
propagation, malformed numeric construction/mutation, identifier validation,
all requested bidi and zero-width characters through caller/stored paths,
renderer failures and non-string redactor inputs. The fake CouchDB GET JSON
encoder uses escaped Unicode so lone-surrogate setting data reaches the real
backend instead of crashing HTTPX's test response builder. Unencodable journal
keys now produce a typed storage diagnostic before reaching SQLite.

Evidence (all in this directory):

- task-5b-fix3-red.log: initial RED, 151 failed / 6 passed.
- task-5b-fix3-journal-red.log: publication/readback detail loss, 2 failed.
- task-5b-fix3-suite.log: first full run, 13 failed / 8932 passed; failures
  resolved as described below.
- task-5b-fix3-suite-final.log: 8967 passed, 7 skipped, 3 subtests passed,
  95.90 seconds. This includes the previous corpus, expanded corpus, existing
  differential, outcome/loss and revision-contract suites.
- task-5b-fix3-build.log and task-5b-fix3-diff-check.log: clean exit 0.
- task-5b-fix3-verification.json: machine-readable result and evidence hashes.

First full-run failures, all resolved in the final full run:

- tests/test_diagnostic_sources.py::test_transport_failure_does_not_reflect_bucket_configuration
- tests/test_diagnostic_whitelist.py::test_real_replay_hostile_name[fresh-17]
- tests/test_diagnostic_whitelist.py::test_real_setting_values_hidden[couchdb-fresh-17]
- tests/test_storage_couchdb.py::test_startup_check_db_missing
- tests/test_storage_couchdb.py::test_auth_error_maps_to_storage_unavailable
- tests/test_storage_couchdb.py::test_server_error_maps_to_storage_unavailable
- tests/test_storage_couchdb.py::test_read_note_on_missing_db_raises
- tests/test_storage_couchdb.py::test_all_docs_on_missing_db_raises
- tests/test_storage_couchdb.py::test_startup_check_db_gone_before_milestone_raises
- tests/test_storage_couchdb.py::test_write_409_concurrent
- tests/test_storage_couchdb.py::test_delete_note_stale_rev_raises
- tests/test_storage_couchdb.py::test_rename_target_race_keeps_source_live
- tests/test_tools_write.py::test_update_requires_mode

Old assertions were updated only where P-21 changes the public contract
(HTTP versus URL, typed operation/cause versus hidden object key, exact JSON
path and integer replay seconds). Safe existing reason wording was retained.
The argument-validation regression was fixed in the wrapper. The two new
lone-surrogate routes required the fixture/backend boundary fixes above.

[DEFER] Brief inconsistency: SECRET matches the prescribed identifier regex;
it is retained, as are other valid identifiers. Rejecting that particular name
would require a separately specified cause-class inventory. This follows the
explicit regex rather than the contradictory parenthetical test example.
Fractional syncMaxSizeInMB diagnostic values are <invalid> under P-21's strict
integer contract; the admission policy itself still supports fractional sizes.
No live storage was contacted. Seven existing optional skips remain. No new
upstream oracle was introduced (P-7); tweak assessment and its fixtures were
not changed. No docs/test-artifacts path was changed or added (P-20).
