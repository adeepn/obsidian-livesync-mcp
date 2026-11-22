# Upstream compatibility pin
plugin: 1.0.50
commonlib: 484c164450d61deb2e301582b60cb9b5f9fa977f

The plugin pin was live-validated against a real client on 2026-11-20.
The commonlib pin is initialized by `scripts/check_upstream.sh --init-pin`
from the current default branch; it is a tracking baseline, not a new audit.
Audited references: docs/AUDIT-2026-11-10.md §2, plugin 517f5c2 /
commonlib 597b64e (2026-11-10). Live acceptance: plugin 1.0.50.

On drift: read the issue diff, update the pin via PR, extend codec tests or
capture a new golden journal fixture from a real client, and note the change
in CHANGELOG. A release-only alert still needs review. Exit codes: 0 clean,
2 plugin release drift, 3 protocol diff, 4 check error. Commonlib changes
outside the manifest are reported but do not trigger an issue alone.
