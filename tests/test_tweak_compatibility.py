"""Pinned commonlib tweak semantics and plugin auto-alignment policy."""
import copy
import pytest
import livesync_format

def assess_tweak_compatibility(*args, **kwargs):
    return livesync_format.assess_tweak_compatibility(*args, **kwargs)

def resolve_tweak_startup(*args, **kwargs):
    return livesync_format.resolve_tweak_startup(*args, **kwargs)

# Literal TweakValuesShouldMatchedTemplate (C:tweak.compatibility.unit.spec.ts).
FULL = dict(minimumChunkSize=20, longLineThreshold=250, encrypt=False,
    usePathObfuscation=False, idDerivationVersion=0, pathNormalizationVersion=0,
    encryptInternalMetadata=False, enableCompression=False, useEden=False,
    customChunkSize=0, useDynamicIterationCount=False, hashAlg='xxhash64',
    enableChunkSplitterV2=False, maxChunksInEden=10, maxTotalLengthInEden=1024,
    maxAgeInEden=10, usePluginSyncV2=False, handleFilenameCaseSensitive=False,
    useSegmenter=False, E2EEAlgorithm='v2', chunkSplitterVersion='v3-rabin-karp')


def relation(result, key):
    return next(entry.relation for entry in result.entries if entry.key == key)


@pytest.mark.parametrize('preferred,current,key,expected', [
    ({}, {}, 'customChunkSize', 'unadvertised'),
    ({}, {'customChunkSize': 0}, 'customChunkSize', 'unadvertised'),
    ({}, {'pathNormalizationVersion': 0}, 'pathNormalizationVersion', 'equal'),
    ({}, {'usePluginSyncV2': False}, 'usePluginSyncV2', 'equal'),
    ({}, {'handleFilenameCaseSensitive': False}, 'handleFilenameCaseSensitive', 'equal'),
    ({}, {'handleFilenameCaseSensitive': True}, 'handleFilenameCaseSensitive', 'different'),
    ({}, {'encryptInternalMetadata': True}, 'encryptInternalMetadata', 'different'),
    ({}, {'E2EEAlgorithm': 'v2'}, 'E2EEAlgorithm', 'equal'),
    ({'enableCompression': True}, {'enableCompression': False}, 'enableCompression', 'different'),
    ({'customChunkSize': 10}, {'customChunkSize': 0}, 'customChunkSize', 'different'),
    ({'hashAlg': 'xxhash64'}, {'hashAlg': ''}, 'hashAlg', 'different'),
    ({'idDerivationVersion': 0}, {'idDerivationVersion': 1}, 'idDerivationVersion', 'different'),
    ({'encrypt': False}, {'encrypt': False, 'idDerivationVersion': 1}, 'idDerivationVersion', 'equal'),
    ({'encrypt': True, 'usePathObfuscation': False},
     {'encrypt': True, 'usePathObfuscation': False, 'idDerivationVersion': 1}, 'idDerivationVersion', 'equal'),
])
def test_upstream_relations(preferred, current, key, expected):
    before = copy.deepcopy((preferred, current))
    result = assess_tweak_compatibility(preferred, current)
    assert relation(result, key) == expected
    assert (preferred, current) == before


@pytest.mark.parametrize('change,status', [
    ({}, 'OK'), ({'customChunkSize': 60}, 'LOSSY_AUTO_ACCEPTABLE'),
    ({'hashAlg': 'xxhash32'}, 'LOSSY_AUTO_ACCEPTABLE'),
    ({'chunkSplitterVersion': 'v1'}, 'LOSSY_AUTO_ACCEPTABLE'),
    ({'enableCompression': True}, 'MISMATCHED'),
    ({'customChunkSize': 60, 'usePluginSyncV2': True}, 'MISMATCHED'),
    ({'pathNormalizationVersion': 1}, 'PATH_IDENTITY_MISMATCH'),
])
def test_classification(change, status):
    result = assess_tweak_compatibility(FULL, {**FULL, **change})
    assert result.status == status
    assert set(result.differing_keys) == set(change)


def test_id_precedes_path_and_gate():
    preferred = {**FULL, 'encrypt': True, 'usePathObfuscation': True}
    result = assess_tweak_compatibility(preferred,
        {**preferred, 'idDerivationVersion': 1, 'pathNormalizationVersion': 1})
    assert result.status == 'ID_KEY_MISMATCH'
    assert 'idDerivationVersion' not in result.adopt_preferred.changes
    assert result.adopt_preferred.reconstruction == 'required'


def test_unadvertised_prevents_lossy_auto_accept():
    preferred = {k: v for k, v in FULL.items() if k != 'minimumChunkSize'}
    result = assess_tweak_compatibility(preferred, {**preferred, 'customChunkSize': 60})
    assert result.status == 'MISMATCHED'


def test_representation_and_legacy_migration():
    result = assess_tweak_compatibility({'encrypt': False},
        {'encrypt': False, 'pathNormalizationVersion': 0})
    assert result.status == 'OK'
    assert result.representation_differs
    assert result.adopt_preferred.reconstruction == 'none'
    legacy = {**FULL, 'chunkSplitterVersion': '', 'enableChunkSplitterV2': True, 'useSegmenter': True}
    normalized = {**legacy, 'chunkSplitterVersion': 'v2-segmenter'}
    assert assess_tweak_compatibility(normalized, legacy).status == 'OK'


@pytest.mark.parametrize('remote,current,winner', [
    (1, 2, 'CURRENT'), (2, 1, 'REMOTE'), (2, 2, 'REMOTE'),
    (None, None, 'REMOTE'), (None, 3, 'CURRENT'), (3, None, 'REMOTE'),
    (0, -1, 'REMOTE'), (-1, 3, 'CURRENT'), ('4', 3, 'CURRENT'),
    (True, None, 'REMOTE'),
])
def test_lossy_winner(remote, current, winner):
    preferred = {**FULL, 'tweakModified': remote}
    device = {**FULL, 'customChunkSize': 60, 'tweakModified': current}
    result = resolve_tweak_startup({'tweak_values': {'PREFERRED': preferred, 'device': device}})
    assert result.status == 'LOSSY_AUTO_ACCEPTABLE'
    assert result.settings['customChunkSize'] == (60 if winner == 'CURRENT' else 0)
    assert result.winners == (winner,)


@pytest.mark.parametrize('key', ['enableCompression', 'pathNormalizationVersion', 'idDerivationVersion'])
def test_pre_gate_checks(key):
    from livesync_format import UnsupportedVaultSettings
    preferred = {**FULL, 'encrypt': True, 'usePathObfuscation': True} if key == 'idDerivationVersion' else FULL
    device = {**preferred, key: 1 if key != 'enableCompression' else True}
    milestone = {'tweak_values': {'PREFERRED': preferred, 'device': device}}
    if key == 'enableCompression':
        assert resolve_tweak_startup(milestone, disable_checking_config_mismatch=True).status == 'MISMATCHED'
    else:
        with pytest.raises(UnsupportedVaultSettings, match=key):
            resolve_tweak_startup(milestone, disable_checking_config_mismatch=True)


def test_auto_accept_requires_explicit_true_or_default():
    from livesync_format import UnsupportedVaultSettings
    milestone = {'tweak_values': {'PREFERRED': FULL, 'device': {**FULL, 'customChunkSize': 60}}}
    for flag in (False, None, 1, 'true'):
        with pytest.raises(UnsupportedVaultSettings, match='customChunkSize'):
            resolve_tweak_startup(milestone, auto_accept_compatible_tweak=flag)


def test_startup_assesses_all_devices_and_reports_all_differing_keys():
    milestone = {'tweak_values': {'PREFERRED': FULL,
        'first': {**FULL, 'useEden': True},
        'second': {**FULL, 'enableCompression': True, 'passphrase': 'do-not-print'}}}
    with pytest.raises(livesync_format.UnsupportedVaultSettings) as caught:
        resolve_tweak_startup(milestone)
    assert 'useEden' in str(caught.value) and 'enableCompression' in str(caught.value)
    assert 'do-not-print' not in str(caught.value)


def test_current_adoption_preserves_id_configuration_and_missing_recommendations():
    preferred = {**FULL, 'syncMaxSizeInMB': 128, 'tweakModified': 1}
    device = {**FULL, 'customChunkSize': 60, 'idDerivationVersion': 1, 'tweakModified': 2}
    result = resolve_tweak_startup({'tweak_values': {'PREFERRED': preferred, 'device': device}})
    assert result.settings['idDerivationVersion'] == 0
    assert result.settings['syncMaxSizeInMB'] == 128
    assert result.settings['customChunkSize'] == 60


def test_json_numeric_precision_matches_javascript():
    # Independent review executed these JSON integer literals through Node's
    # JSON.parse + pinned assessment/policy: same binary64 Number => equal/REMOTE.
    preferred = {**FULL, 'minimumChunkSize': 9007199254740992}
    current = {**FULL, 'minimumChunkSize': 9007199254740993}
    assert assess_tweak_compatibility(preferred, current).status == 'OK'
    assert livesync_format.select_newer_tweak_side(
        {'tweakModified': 9007199254740993}, {'tweakModified': 9007199254740992}) == 'REMOTE'
