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

## Real-client evidence

The live-validated client remains **1.0.50**, validated **2026-11-20**:
[final transcripts](test-artifacts/2026-11-20-live-s3/) and
[original acceptance checklist](live-check-s3-checklist.md).
That validation covered S3 reconnect/replay, byte comparison and peer writes
reflected by a real client under its settings; it did not validate every
settings combination or plugin 1.0.52. The sanitized real milestone was not
captured then. Native-host invalid-byte decoding remains an open experiment.

Task 8 must update this section only after the
[parity live acceptance](superpowers/plans/2026-11-23-settings-parity.md), including the actual
client version, sanitized settings, large attachments, mutations and decoding
experiments. Preparing a checklist/script is not live validation.

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
