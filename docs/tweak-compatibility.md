# Tweak compatibility at startup

Both backends read `tweak_values.PREFERRED` from the LiveSync milestone.
CouchDB uses `_local/obsydian_livesync_milestone`; S3 uses its milestone object.
Without PREFERRED the peer retains the existing device-settings fallback and
checks all advertised devices against that baseline. Startup compares the peer's
effective settings against PREFERRED and every device snapshot, and each device
against PREFERRED. Semantic differences refuse
startup with key names and an alignment instruction, without printing values.
Unadvertised settings remain distinct from known defaults. Legacy splitter
settings pass through the same one-pass migration used by the writer.

Fully advertised differences limited to `hashAlg`, `customChunkSize` and
`chunkSplitterVersion` can align automatically. The greater positive numeric
`tweakModified` wins; ties or two absent/nonpositive timestamps choose PREFERRED
(REMOTE). The winner is logged and applied to this peer's writer. This startup
check does not change device snapshots or publish PREFERRED. By explicit policy
this peer remains an invisible writer: it publishes note changes without those
milestone writes. CURRENT winners therefore still require remote alignment if
devices are to adopt their settings.

`LIVESYNC_AUTO_ACCEPT_COMPATIBLE_TWEAK` defaults to `true`; `false` refuses
these differences. `LIVESYNC_DISABLE_CHECKING_CONFIG_MISMATCH` defaults to
`false`; `true` bypasses ordinary semantic differences, matching the device's
local flag. ID derivation and path-normalization checks precede this gate.
Encryption and path-obfuscation refusals remain enforced.

CouchDB compression follows PREFERRED, otherwise the first readable settings
map (absent means false; non-booleans refuse with a correction instruction).
Compression disagreement never blocks CouchDB startup: all devices can read both
forms. The peer advertises its effective value, and `vault_stats` reports the
typed boolean `enableCompression`. Compressed reads always decode, including
when the mismatch bypass flag is true. Journal framing does not use this option
and its existing compatibility policy is unchanged. These flags are local
policy, not values inferred from milestone tweaks. Restart after changing them.
`vault_stats` includes `tweak_assessment` with `status` and `differing_keys` only.

Source semantics and the deliberately read-only remote alignment policy are
recorded in the [parity specification](superpowers/specs/2026-11-23-settings-parity.md#tweak-compatibility-and-peer-startup-requirements).
