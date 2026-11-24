"""Device/PREFERRED checks through real backend startup, with fake transports."""
import copy
import json
import logging
from contextlib import asynccontextmanager

import pytest
from storage_base import UnsupportedVault
from tests.test_tweak_compatibility import FULL
from tests.test_storage_couchdb import fake_vault
from tests.test_storage_journal import make_backend, MILESTONE_KEY, SYNC_PARAMETERS_KEY, params_blob


@asynccontextmanager
async def vault(tmp_path, kind, milestone):
    if kind == 'couchdb':
        async with fake_vault({'_local/obsydian_livesync_milestone': milestone}) as (backend, raw, log):
            yield backend, lambda: [r for r in log if r.method in ('PUT', 'DELETE')]
    else:
        backend, storage = make_backend(tmp_path, {MILESTONE_KEY: json.dumps(milestone).encode(),
                                                  SYNC_PARAMETERS_KEY: params_blob()})
        try:
            yield backend, lambda: storage.put_calls
        finally:
            await backend.close()


@pytest.fixture(params=['couchdb', 'journal'])
def kind(request):
    return request.param


@pytest.mark.parametrize('key,value', [('useEden', True), ('enableCompression', True),
    ('minimumChunkSize', 40), ('pathNormalizationVersion', 1),
    ('handleFilenameCaseSensitive', True)])
async def test_semantic_device_mismatch_refuses_without_secret_values(tmp_path, kind, key, value):
    milestone = {'tweak_values': {'PREFERRED': FULL,
        'device': {**FULL, key: value, 'passphrase': 'MUST-NOT-LEAK'}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        if key == 'enableCompression' and kind == 'couchdb':
            await backend.startup_check()
            assert key not in (await backend.backend_info())['tweak_assessment']['differing_keys']
            return
        with pytest.raises(UnsupportedVault) as caught:
            await backend.startup_check()
        assert key in str(caught.value)
        assert 'devices require alignment' in str(caught.value)
        assert 'MUST-NOT-LEAK' not in str(caught.value)
        info = await backend.backend_info()
        assert key in info['tweak_assessment']['differing_keys']
        assert not writes()


@pytest.mark.parametrize('remote,current,expected', [(1,2,60), (2,1,0), (2,2,0),
    (None,None,0), (None,3,60), (3,None,0)])
async def test_lossy_startup_applies_winner_and_logs(tmp_path, kind, caplog, remote, current, expected):
    preferred = {**FULL, 'tweakModified': remote}
    milestone = {'tweak_values': {'PREFERRED': preferred,
        'device': {**FULL, 'customChunkSize': 60, 'tweakModified': current}}}
    original = copy.deepcopy(milestone)
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        with caplog.at_level(logging.INFO):
            await backend.startup_check()
        assert backend._splitter.normalized_settings['customChunkSize'] == expected
        assert 'tweak alignment' in caplog.text and 'customChunkSize' in caplog.text
        assert ('CURRENT' if expected == 60 else 'REMOTE') in caplog.text
        assert (await backend.backend_info())['tweak_assessment'] == {
            'status': 'LOSSY_AUTO_ACCEPTABLE', 'differing_keys': ['customChunkSize']}
        assert milestone == original
        assert not writes()
        await backend.write_note('test.md', 'writer uses chosen settings', create=True)
        assert (await backend.read_note('test.md'))['text'] == 'writer uses chosen settings'


async def test_lossy_disabled_refuses(tmp_path, kind):
    milestone = {'tweak_values': {'PREFERRED': FULL, 'device': {**FULL, 'hashAlg': 'xxhash32'}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        backend.auto_accept_compatible_tweak = False
        with pytest.raises(UnsupportedVault, match='hashAlg'):
            await backend.startup_check()
        assert not writes()


@pytest.mark.parametrize('key', ['useEden', 'pathNormalizationVersion'])
async def test_backend_gate(tmp_path, kind, key):
    milestone = {'tweak_values': {'PREFERRED': FULL, 'device': {**FULL, key: 1 if key == 'pathNormalizationVersion' else True}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        backend.disable_checking_config_mismatch = True
        if key == 'pathNormalizationVersion':
            with pytest.raises(UnsupportedVault, match=key):
                await backend.startup_check()
        else:
            await backend.startup_check()
        assert not writes()


@pytest.mark.parametrize('settings', [ {'pathNormalizationVersion': 0},
    {**FULL, 'customChunkSize': 60, 'usePluginSyncV2': True, 'syncMaxSizeInMB': 128,
     'handleFilenameCaseSensitive': False, 'pathNormalizationVersion': 1},
    {**FULL, 'chunkSplitterVersion': '', 'enableChunkSplitterV2': True, 'useSegmenter': True}])
async def test_healthy_shapes_preserve_writer(tmp_path, kind, settings):
    milestone = {'tweak_values': {'PREFERRED': settings, 'device': copy.deepcopy(settings)}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        await backend.startup_check()
        assert (await backend.backend_info())['tweak_assessment'] == {'status': 'OK', 'differing_keys': []}
        assert not writes()


async def test_no_preferred_detects_device_disagreement(tmp_path, kind):
    milestone = {'tweak_values': {'one': FULL, 'two': {**FULL, 'useEden': True}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        with pytest.raises(UnsupportedVault, match='useEden'):
            await backend.startup_check()
        assert not writes()


async def test_peer_effective_metadata_preference_is_assessed(tmp_path, kind):
    # C:LiveSyncDBFunctions.ts:19-24: plaintext peers advertise false even
    # when the saved UI preference says encryptInternalMetadata=true.
    milestone = {'tweak_values': {'PREFERRED': {**FULL, 'encryptInternalMetadata': True}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        with pytest.raises(UnsupportedVault, match='encryptInternalMetadata'):
            await backend.startup_check()
        assert not writes()


async def test_id_mismatch_precedes_gate_and_encryption_refusal(tmp_path, kind):
    milestone = {'tweak_values': {'PREFERRED': FULL, 'device': {
        **FULL, 'encrypt': True, 'usePathObfuscation': True, 'idDerivationVersion': 1}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        backend.disable_checking_config_mismatch = True
        with pytest.raises(UnsupportedVault, match='idDerivationVersion'):
            await backend.startup_check()
        assert (await backend.backend_info())['tweak_assessment']['status'] == 'ID_KEY_MISMATCH'
        assert not writes()


async def test_gate_cannot_enable_unsupported_encryption(tmp_path, kind):
    milestone = {'tweak_values': {'PREFERRED': {**FULL, 'encrypt': True}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        backend.disable_checking_config_mismatch = True
        with pytest.raises(UnsupportedVault, match='encryption'):
            await backend.startup_check()
        assert not writes()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('override', [None, 10])
async def test_no_preferred_retains_conflicting_size_guard(tmp_path, kind, reverse, override):
    items = [('one', {**FULL, 'syncMaxSizeInMB': 50}),
             ('two', {**FULL, 'syncMaxSizeInMB': 100})]
    milestone = {'tweak_values': dict(reversed(items) if reverse else items)}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        backend.sync_max_size_mb_override = override
        if override is None:
            with pytest.raises(UnsupportedVault, match='conflicting syncMaxSizeInMB'):
                await backend.startup_check()
        else:
            await backend.startup_check()
            assert backend.sync_max_size_mb == 10
        assert not writes()


async def test_peer_is_also_compared_to_each_device(tmp_path, kind):
    milestone = {'tweak_values': {'PREFERRED': {'pathNormalizationVersion': 0},
        'device': {'pathNormalizationVersion': 0, 'minimumChunkSize': 80, 'hashAlg': 'sha1'}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        with pytest.raises(UnsupportedVault) as caught:
            await backend.startup_check()
        assert 'minimumChunkSize' in str(caught.value) and 'hashAlg' in str(caught.value)
        assert not writes()


@pytest.mark.parametrize('key,value', [('encryptInternalMetadata', False), ('maxAgeInEden', 10),
    ('enableCompression', False), ('longLineThreshold', 250)])
async def test_sparse_compared_settings_are_readable(tmp_path, kind, key, value):
    milestone = {'tweak_values': {'PREFERRED': {key: value}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        await backend.startup_check()
        assert (await backend.backend_info())['tweak_assessment']['status'] == 'OK'
        assert not writes()


async def test_couchdb_effective_compression_preference_follows(tmp_path):
    milestone = {'tweak_values': {'PREFERRED': {**FULL, 'enableCompression': True}}}
    async with vault(tmp_path, 'couchdb', milestone) as (backend, writes):
        await backend.startup_check()
        assert backend.enable_compression is True
        assert not writes()


async def test_newest_matching_device_can_win_lossy_alignment(tmp_path, kind):
    milestone = {'tweak_values': {'PREFERRED': {**FULL, 'tweakModified': 1},
        'older': {**FULL, 'customChunkSize': 60, 'tweakModified': 2},
        'newer': {**FULL, 'customChunkSize': 0, 'tweakModified': 3}}}
    async with vault(tmp_path, kind, milestone) as (backend, writes):
        await backend.startup_check()
        assert backend._splitter.normalized_settings['customChunkSize'] == 0
        assert not writes()
