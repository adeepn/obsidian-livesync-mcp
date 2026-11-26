# Tweak compatibility at startup

For compatibility-compared keys, both backends read
`tweak_values.PREFERRED` from the LiveSync milestone.
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

The pinned recommendation template also includes recommendation-only
`useCustomRequestHandler`, `batch_size`, `batches_limit`, `useTimeouts`,
`readChunksOnline`, `hashCacheMaxCount`, `hashCacheMaxAmount`,
`concurrencyOfReadChunksOnline`, and `minimumIntervalOfReadChunksOnline`.
These are device transport/cache scheduling preferences, not peer writer-policy
inputs. The peer does not derive them from the milestone. Other template keys
also in TweakCompatibilityRules remain compatibility-compared.

Source: C:common/models/tweak.definition.ts:24-67,116-135;
C:common/models/tweak.compatibility.ts:153-201;
P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:120-132,403-405.
The native witness in `tests/fixtures/recommendation_upstream.json` executes
these comparisons and the matched early-return branch. Multi-device arbitration
is the owner's P-48 peer policy, not an invented upstream device oracle.

## Recommendation-only writer policy

`syncMaxSizeInMB`, `useIgnoreFiles`, and `ignoreFiles` resolve independently
from device maps advertising that key, before consulting PREFERRED. An explicit
operator override always wins. Equal effective device policies use that value;
disagreement uses the greatest positive numeric `tweakModified`. Under P-48a,
a tie at that greatest positive timestamp counts as a tie only when the tied
newest devices disagree: equally newest devices that agree give their common
value even when older devices differ. If equally newest devices disagree, or
no device has a positive timestamp, use PREFERRED when it advertises the key.
Otherwise issue a typed ambiguity refusal naming the setting and operator override:
`LIVESYNC_SYNC_MAX_SIZE_MB`, `LIVESYNC_USE_IGNORE_FILES`, or
`LIVESYNC_IGNORE_FILES`. When no device advertises a key, use PREFERRED, then
the device default. JavaScript size coercion, boolean truthiness and comma-name
splitting still apply. Only maps advertising the individual key vote.

This follows recommendation-only device behavior, not the compatibility
baseline: required and compatible-lossy settings retain their existing
PREFERRED-based assessment. Journal refresh validates the selection, size,
derived text budget and provenance before installing the complete candidate.
Rejected candidates preserve the previous effective snapshot. The peer never
registers a device or publishes PREFERRED.

A device keeps its own recommendation-only values while its settings match
PREFERRED: the matched branch returns before changing anything. When the user
accepts the other side of a compatibility mismatch, the plugin copies the
adopted side's recommendation-only values together with the compared changes
(P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:65-81,403-405,414).
Recommendation-only values therefore move between a device and PREFERRED only
through such a resolution. A plain settings change republishes only that
device's own map. It was observed live on 2026-11-26 with plugin 1.0.50: the
device map held `syncMaxSizeInMB` 100 while PREFERRED still held 50, and the
device uploaded a 100 MiB file. The peer reads the device maps first for that
reason; the policy above does not depend on the adoption path.

`backend_info` and `obsidian_vault_stats` expose `recommendation_policy`, keyed
by these three setting names. Each entry contains `source` (`override`,
`device`, or `preferred`), integer `device_count`, and boolean `disagree`.
Size and use-ignore entries expose the resolved numeric/boolean `value`;
ignore names expose only integer `name_count`. These two values are the only settings
values in tool output, and each is closed: a finite number or a boolean, never text
from a setting. A nonfinite unlimited size is
reported as JSON null rather than a nonstandard numeric token. Device IDs and names are never
exposed. `preferred` also labels the default when nothing advertises a key.
The legacy selection source fields retain their `explicit`/`vault` vocabulary.
