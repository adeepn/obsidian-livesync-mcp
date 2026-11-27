# Upstream compatibility pin
plugin: 1.0.52
commonlib: 484c164450d61deb2e301582b60cb9b5f9fa977f

## Source audit

Settings-parity audited plugin **1.0.52**, commit
`5070b67beae3b60e3a79132386a08da3f1ef213a`, and commonlib at the exact
commit above. This is a source-audited compatibility baseline, not merely a
tracking pin. See the [settings extraction](superpowers/specs/2026-11-23-settings-parity.md).

Source-audited/offline-oracle behavior covers identity combinations, every
splitter/migration and hash mode, disk-ingestion types, size predicates,
selection filters, tweak compatibility, revision replay and wipe/epoch recovery.
The committed fixtures execute the pinned upstream sources with the real
`octagonal-wheels@0.1.57` decoder. Tests consume fixtures offline without Node.
Fixture regeneration needs Node 26.13.1+ (TypeScript stripping); active selection
filters separately require Node 22+ at server runtime.

The current baseline is **source-audited + oracle-differential + real-server
automated acceptance done**. The [2026-11-25 Docker acceptance summary](test-artifacts/2026-11-25-parity-docker/summary.txt)
records 261 passing real-server checks. This includes typed CouchDB lone-surrogate
refusals and JWT wrong-key/expired-token authorization errors; it does not certify
a real Obsidian client's filesystem reflection under every setting.

Every generator, exact dependency acquisition/integrity procedure and validating
test is catalogued in [Regenerating oracle fixtures](regenerating-oracle-fixtures.md).
Ordinary oracle tests are offline; active-selection integration tests invoke Node.

## Real-client evidence

The live-validated client is **1.0.50**. The settings-parity live acceptance ran
against an S3 journal on **2026-11-25 and 2026-11-26**.
Automated real-server acceptance: **DONE** —
[261 checks](test-artifacts/2026-11-25-parity-docker/summary.txt).
Live Obsidian acceptance: **DONE** for the main scenarios of the
[parity live checklist](live-check-parity-checklist.md). The
[record](test-artifacts/2026-11-26-parity-live/summary.txt) lists every step, the
defects the run found and the limits of the evidence. It shows:

- every file the comparison admits equal to what the client stored (bytes, type,
  metadata size, piece ids), after the reconnect and again after the owner changed the
  size limit on the device; hidden files and files above the size limit are outside the
  comparison, and the one file with invalid UTF-8 is compared after the client replaced
  the invalid bytes (15 bytes on disk, 21 stored);
- 40 MiB and 100 MiB client-written binaries and a peer-written 40 MiB binary
  applied by the client;
- peer-written create, update, rename and soft delete applied by the client; the
  revision prefixes the client printed match the bucket's revision trees; nothing
  sent back;
- a `.md` file with invalid UTF-8 ingested by the client as plain text, each
  invalid sequence replaced by U+FFFD (15 bytes became 21), matching the peer;
- a 50 MiB + 1 byte upload skipped and the rename of an oversized file refused,
  with the bucket unchanged.

The additional checklist scenarios were not executed. The large files repeat a
short pattern, so the client cut them at the maximum piece size: cut positions on
varied bytes rest on the offline oracle differentials, not on this run. One device,
one vault, one remote type and one splitter and hash mode were live, and the lossless
legacy cut has no live evidence. CouchDB has no live client run; its evidence is the
automated real-server acceptance. **1.0.52 has not been exercised live**.

First owner observation (2026-11-25): ordinary sync against a confirmed wiped
bucket creates service records (milestone and sync parameters) but does not
re-send files; see step 0 of the checklist for the explicitly authorized
device-to-server rebuild sequence.

The earlier **2026-11-20** S3 validation stays on record:
[final transcripts](test-artifacts/2026-11-20-live-s3/) and the
[original acceptance checklist](live-check-s3-checklist.md). The sanitized real
milestone was not captured then.

Earlier historical audit: `docs/AUDIT-2026-11-10.md` §2, plugin `517f5c2` /
commonlib `597b64e` (2026-11-10).

## Drift checks

Run `scripts/check_upstream.sh` with authenticated `gh`. The daily/manual
watcher compares plugin releases and commonlib protocol-manifest changes and
maintains one `upstream-drift` issue; failed checks do not close alerts.
On drift, read the diff, update the pin through a PR, extend tests or capture
a real-client golden journal, and record the change in CHANGELOG.
Do not advance the pin merely to silence an alert.

Exit codes: 0 clean, 2 release drift, 3 protocol drift, 4 check error.
Commonlib changes outside the manifest are reported without an issue by
themselves. `--init-pin` only bootstraps an explicitly uninitialized pin.


## 2026-11-25 acceptance corrections

The 2026-11-25 acceptance run found JWT wrong-key HTTP 400 classified as a
generic storage error and CouchDB rejecting a lone-surrogate leaf with HTTP 400.
Both production paths are corrected; the final automated acceptance covers
text and binary in every matrix row, 40 MiB and 100 MiB binary round trips,
a 201 MiB skip and a 12 MiB textual MCP workflow. See the [recorded results](test-artifacts/2026-11-25-parity-docker/summary.txt).

Real-server coverage includes the 60-row pairwise matrix on both backends.
It does not establish every setting combination on every server.
