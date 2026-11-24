# Independent read-only review of Task 5b fix2

Scope: current uncommitted diff against HEAD on settings-parity, inspected 2026-11-24. No repository edits, network, hardware, production calls or full-suite runs. Parent is concurrently editing; line numbers below identify the inspected snapshot. Reviewed prior independent report /tmp/parity-rereview-report.md and current typed construction, backend_info, refresh, generic exception handling and default logger sites.

## Findings

1. P2: backend observability still returns free remote/configuration text. storage_journal.py:400-404 builds protocol_notes from arbitrary unknown remote JSON field names and :448 returns it unchanged; :449 returns configured state_dir_path. storage_couchdb.py:463 returns configured database and :465 returns arbitrary remote update_seq. server.py:1334 exposes configured state_dir_path in vault_stats. These were existing fields retained by the diff; they violate the expanded P19 backend-info projection boundary. Parent notified and confirmed work in progress to replace with count/hidden/fingerprint fields.

2. P2: generic exceptions still escape the tool wrapper. server.py:333-334 ends its exception inventory after FrontmatterError. storage_journal.py:386-388 explicitly rethrows arbitrary refresh errors; CouchDB response.json() calls (:242, :257, :284, :314) can produce non-StorageError failures. This prevents the typed projection from being the universal tool boundary and permits framework default exception formatting/logging for failures containing remote data. Existing uncovered route, not newly introduced. Parent confirmed work in progress for a class-only generic boundary. No claim that ordinary JSONDecodeError strings themselves echo entire bodies.

3. P3: caller control escaping is incomplete in diagnostics. server.py:1408 empty template list result interpolates raw folder; :1218 canvas node success interpolates caller-supplied node_id through new_id; :1292 canvas edge success does likewise for edge_id. For a caller identifier containing newline, these return literal newlines while corresponding missing/duplicate branches escape controls. These are existing echoes retained in partially updated areas; they disclose no new remote secret. Use escape_path on these three values. Parent notified.

## Other assessment

- Public reason inventory, setting-key inventory and value allowlist remove arbitrary free-text setting projection. Cause fields use exception classes.
- Journal filename projection admits only the two specified shapes; otherwise only length survives.
- Current last-line redactor is fixed-budget and returns constant redacted output for nonempty input; previous unbounded regex path is removed.
- Default logging in changed storage/selection modules excludes remote bodies, exception strings, regex details and tracebacks. No additional arbitrary diagnostic interpolation found in inspected logger sites.
- OutcomeUnknown preserves uncertainty and operation-specific retry guidance; no tweak comparison or assessment decision logic change identified in reviewed diff.
- Current tests exercise StorageError subclasses but their generic-exception coverage was missing at initial inspection.

Status: findings sent to parent for concurrent fixes; this report records the inspected snapshot and does not certify later mutations. No additional remote-secret route beyond findings 1-2 identified in this pass.

# Follow-up review after concurrent closures

The original free-text backend_info routes and generic exception boundary are closed in the refreshed snapshot. Remote journal metadata names are count-only; database/state directory hidden; update_seq and epoch fingerprints. The template/canvas success control-escaping omissions are closed. Additional doc_count string/object route was reported to parent and verified closed by an HTTP-level fake returning doc_count="RemoteSecretMarker": backend_info now returns "<value hidden>".

Targeted tests: tests/test_diagnostic_whitelist.py and tests/test_storage_messages.py, 977 passed in 3.89s. Durable output: /tmp/parity-fix2-independent-targeted.log. No full suite run by reviewer.

Remaining concerns reported:

- Caller-origin path provenance: DiagnosticPath defaults remote=True, including RenamePartialError.source_path/destination_path (storage_base.py) and several CouchDB raises accepting caller path/new_path. A direct constructor probe with caller e+combining acute `.md` produced NFC `é.md`, contrary to caller-path preservation. Remote decode diagnostics correctly should retain remote=True. Parent asked to annotate caller constructions explicitly.
- Safe typed details are discarded by exact-class dispatch: server.py:262 returns static messages for exact UnsupportedVault/StorageUnavailable and other mapped classes without consulting the typed diagnostic. HEAD appended safe diagnostic details; the diff removes that. An UnsupportedVault carrying TWEAK_ALIGNMENT/differing_keys therefore becomes only "Error: unsupported vault configuration." This loses actionable typed fields despite closing arbitrary text reflection. Parent asked to confirm/fix; existing revised tests assert arbitrary free text is absent but do not prove typed specific reasons remain visible.

No further arbitrary remote exception/body/header/configuration/regex/ignore/milestone text path identified in reviewed default log calls or typed diagnostic construction. PartB decision/control flow remains unchanged: exception-only fields added around existing decisions.

# Final delta review — closure

Rechecked only the final fixes to prior findings. All prior findings are closed in the inspected final snapshot:

- DiagnosticPath.remote is required; all production constructors now choose provenance explicitly. Journal/document decode fields use remote=True; caller-origin errors/outcomes use remote=False. RenamePartialError wrapper additionally prefers original caller arguments, preserving decomposed Unicode and escaped controls.
- Exact mapped StorageError classes append only PublicDiagnostic with a non-generic closed enum reason or a cause class; raw exception text remains excluded. TWEAK_ALIGNMENT includes known keys and device alignment guidance. UnsupportedVaultSettings fallback uses a fixed SETTINGS_UNREADABLE reason plus exception class.
- doc_count is exposed only for exact nonnegative int values; other values are hidden.
- No remaining actionable concern found in these deltas. Tweak assessment control flow remains unchanged.

Reviewer rerun: tests/test_diagnostic_whitelist.py + tests/test_storage_messages.py, 978 passed in 3.38s. Durable log: /tmp/parity-fix2-independent-final-targeted.log. No full suite, network, hardware or repository edits performed by reviewer.

Final reviewer verdict: NO_NEW_FINDINGS. Full-suite completion remains the parent's verification responsibility.
