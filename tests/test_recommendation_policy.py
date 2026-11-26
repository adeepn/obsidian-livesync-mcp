"""F14 / P-48 policy arbitration (peer rule, not an upstream oracle).

C:common/models/tweak.definition.ts:116-135; tweak.compatibility.ts:153-201.
P:modules/coreFeatures/ModuleResolveMismatchedTweaks.ts:120-132,403-405.
Owner live observation (NOT oracle): plugin 1.0.50 uploaded its own 100 MiB
file with device=100/1795681969510 and PREFERRED=50/1795001730894.
"""
import copy
import json
import math
import pytest
import livesync_format as fmt
from tests.test_tweak_startup import vault
from tests.test_storage_journal import make_backend, healthy_bucket, MILESTONE_KEY
from storage_base import UnsupportedVault

KEYS = [('syncMaxSizeInMB', 50, 100, 'LIVESYNC_SYNC_MAX_SIZE_MB'),
        ('useIgnoreFiles', False, True, 'LIVESYNC_USE_IGNORE_FILES'),
        ('ignoreFiles', 'old', 'new', 'LIVESYNC_IGNORE_FILES')]


def resolve(m, key, override=None):
    if key == 'syncMaxSizeInMB':
        return fmt.resolve_sync_max_size(m, override=override)
    use, names = fmt.resolve_ignore_settings(m,
        use_override=override if key == 'useIgnoreFiles' else True,
        names_override=(override,) if override is not None and key == 'ignoreFiles' else None)
    return use if key == 'useIgnoreFiles' else names[0]


@pytest.mark.parametrize('key,old,new,env', KEYS)
@pytest.mark.parametrize('case', ['single', 'newest', 'tie', 'no-positive', 'unadvertised',
                                 'no-preferred', 'newest-no-preferred', 'ambiguous', 'equal', 'tied-equal', 'top-equal'])
def test_resolution_matrix(key, old, new, env, case):
    p = {key: old, 'pathNormalizationVersion': 0}
    a = {key: new, 'tweakModified': 20}
    b = {key: old, 'tweakModified': 10}
    entries = {'PREFERRED': p, 'a': a}
    want = new
    if case in ('newest', 'tie', 'no-positive', 'ambiguous', 'equal', 'tied-equal', 'newest-no-preferred', 'top-equal'):
        entries['b'] = b
    if case in ('tie', 'ambiguous'):
        b['tweakModified'] = 20
        want = old
    if case == 'no-positive':
        a['tweakModified'] = 0
        b['tweakModified'] = -1
        want = old
    if case == 'unadvertised':
        a.pop(key)
        want = old
    if case in ('no-preferred', 'newest-no-preferred', 'ambiguous'):
        entries.pop('PREFERRED')
    if case in ('equal', 'tied-equal'):
        b[key] = new
        if case == 'tied-equal':
            b['tweakModified'] = 20
    if case == 'top-equal':
        entries['c'] = {key: new, 'tweakModified': 20}
    m = {'tweak_values': entries}
    before = copy.deepcopy(m)
    if case == 'ambiguous':
        with pytest.raises(fmt.UnsupportedVaultSettings, match=env) as caught:
            resolve(m, key)
        assert key in str(caught.value)
        assert caught.value.diagnostic is not None
    else:
        assert resolve(m, key) == want
    assert resolve(m, key, old) == old
    assert m == before


def live(size=100):
    return {'tweak_values': {
        'PREFERRED': {'pathNormalizationVersion': 0, 'syncMaxSizeInMB': 50,
                      'tweakModified': 1795001730894},
        'private-device-id': {'pathNormalizationVersion': 0, 'syncMaxSizeInMB': size,
                              'tweakModified': 1795681969510}}}


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_live_policy_startup_and_public_stats(tmp_path, kind):
    async with vault(tmp_path, kind, live()) as (b, writes):
        await b.startup_check()
        assert b.sync_max_size_mb == 100
        assert b.text_scan_max_bytes == 256 * 1024**2
        info = await b.backend_info()
        assert info['recommendation_policy']['syncMaxSizeInMB'] == {
            'value': 100, 'source': 'device', 'device_count': 1, 'disagree': False}
        assert 'private-device-id' not in json.dumps(info)
        assert not writes()
        data = b'\x00' * (60 * 1024**2)
        meta = await b.write_attachment('large.pdf', data, create=True)
        assert meta['size'] == len(data)
        data = b'\x00' * (100 * 1024**2)
        await b.write_attachment('large.pdf', data)
        await b.rename_note('large.pdf', 'renamed.pdf')
        assert len((await b.read_note('renamed.pdf'))['data']) == len(data)


async def test_refresh_policy_and_budget_are_atomic(tmp_path):
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        for size in (100, 300, 50):
            storage._objects[MILESTONE_KEY] = json.dumps(live(size)).encode()
            await b.refresh(force=True)
            assert b.sync_max_size_mb == size
            assert b.text_scan_max_bytes == max(256, size) * 1024**2
            before = copy.deepcopy((await b.backend_info())['recommendation_policy'])
            assert before['syncMaxSizeInMB']['value'] == size
            assert before['syncMaxSizeInMB']['source'] == 'device'
            rejected = live(400)
            rejected['tweak_values'].pop('PREFERRED')
            rejected['tweak_values']['second'] = {'syncMaxSizeInMB': 1, 'tweakModified': 1795681969510}
            storage._objects[MILESTONE_KEY] = json.dumps(rejected).encode()
            with pytest.raises(UnsupportedVault, match='LIVESYNC_SYNC_MAX_SIZE_MB'):
                await b.refresh(force=True)
            assert b.sync_max_size_mb == size
            assert b.text_scan_max_bytes == max(256, size) * 1024**2
            assert (await b.backend_info())['recommendation_policy'] == before
    finally:
        await b.close()


def test_native_recommendation_witness():
    from pathlib import Path
    from tests.test_tweak_differential import FIXTURE
    data = json.loads((Path(__file__).parent / 'fixtures/recommendation_upstream.json').read_text())
    compared = {rule['key'] for rule in FIXTURE['rules']}
    assert set(data['recommendedOnly']) == set(FIXTURE['selection']['templateKeys']) - compared - {'tweakModified'}
    assert set(data['recommendedOnly']) & compared == set()
    assert set(key for key, *_ in KEYS) <= set(data['recommendedOnly'])
    for case in data['vectors']:
        assert case['alignment'] == 'matched'
        assert case['differences'] == []
        assert case['result'] == {'result': False, 'requireFetch': False}
        assert case['retained'] == case['current'][case['key']]
        assessment = fmt.assess_tweak_compatibility(case['preferred'], case['current'])
        assert assessment.alignment == case['alignment']
        assert list(assessment.differing_keys) == case['differences']


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_policy_stats_are_closed_and_include_all_keys(tmp_path, kind, monkeypatch):
    import server
    m = live()
    m['tweak_values']['private-device-id'].update(useIgnoreFiles=False, ignoreFiles='secret-name')
    async with vault(tmp_path, kind, m) as (b, writes):
        await b.startup_check()
        monkeypatch.setattr(server, 'backend', b)
        result = json.loads(await server.obsidian_vault_stats())
        policy = result['recommendation_policy']
        assert policy['useIgnoreFiles'] == dict(value=False, source='device', device_count=1, disagree=False)
        assert policy['ignoreFiles'] == dict(name_count=1, source='device', device_count=1, disagree=False)
        assert 'secret-name' not in json.dumps(result)
        assert 'private-device-id' not in json.dumps(result)


async def test_public_policy_rejects_untyped_fields(monkeypatch):
    from diagnostic_hygiene import recommendation_fields
    closed = dict(source=None, device_count=0, disagree=False)
    bad = {'syncMaxSizeInMB': dict(value='secret', source='secret', device_count=True, disagree='secret'),
           'useIgnoreFiles': dict(value='secret', source='secret', device_count=1.5, disagree=1),
           'ignoreFiles': dict(value='secret', name_count='secret', source='secret', device_count=-1, disagree='secret'),
           'unexpected-key': dict(value='secret')}
    projected = recommendation_fields(bad)
    assert 'secret' not in json.dumps(projected)
    # Every field of the projection falls back to a closed default on a wrong type.
    assert projected == {'syncMaxSizeInMB': dict(closed, value=None),
                         'useIgnoreFiles': dict(closed, value=None),
                         'ignoreFiles': dict(closed, name_count=0)}
    assert recommendation_fields('secret') == {}
    assert recommendation_fields({'syncMaxSizeInMB': 'secret', 'useIgnoreFiles': dict(closed, value=True)}) == {
        'useIgnoreFiles': dict(closed, value=True)}


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_unconvertible_device_size_names_the_override(tmp_path, kind):
    m = live()
    m['tweak_values']['private-device-id']['syncMaxSizeInMB'] = {'toString': 5}
    async with vault(tmp_path, kind, m) as (b, writes):
        with pytest.raises(UnsupportedVault, match='LIVESYNC_SYNC_MAX_SIZE_MB') as caught:
            await b.startup_check()
        assert 'syncMaxSizeInMB' in str(caught.value)
        assert 'private-device-id' not in str(caught.value)
        assert not writes()
        # An operator override bypasses the remote conversion.
        b.sync_max_size_mb_override = 60
        await b.startup_check()
        assert b.sync_max_size_mb == 60
        assert not writes()


# The resolved size and the ignore switch are the only settings values in tool
# output; each is a finite number or a boolean, never text from a device.
DEVICE_VALUES = ['private-text', ['private-text'], {'valueOf': 'private-text'}, '  ', '0x1F', True,
                 None, -5, 1e308, 'Infinity', '', [50], [[['60']]], ' 1e3 ']


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
@pytest.mark.parametrize('raw', DEVICE_VALUES)
async def test_public_policy_values_are_closed_numbers_or_booleans(tmp_path, kind, raw):
    m = live()
    m['tweak_values']['private-device-id'].update(syncMaxSizeInMB=raw, useIgnoreFiles=raw)
    async with vault(tmp_path, kind, m) as (b, writes):
        await b.startup_check()
        policy = (await b.backend_info())['recommendation_policy']
        text = json.dumps(policy, allow_nan=False)
        size = policy['syncMaxSizeInMB']['value']
        assert size is None or (type(size) in (int, float) and 0 <= size < math.inf)
        assert type(policy['useIgnoreFiles']['value']) is bool
        assert 'private-text' not in text
        assert 'private-device-id' not in text


async def test_capability_rejection_preserves_complete_policy(tmp_path, monkeypatch):
    from storage_base import WriterUnsupported
    from diagnostic_hygiene import PublicDiagnostic, DiagnosticCode
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        old = (b.sync_max_size_mb, b.text_scan_max_bytes, b.selection_use_ignore_files,
               b.selection_ignore_files, copy.deepcopy(b._recommendation_policy))
        m = live(400)
        m['tweak_values']['private-device-id'].update(useIgnoreFiles=True, ignoreFiles='changed')
        storage._objects[MILESTONE_KEY] = json.dumps(m).encode()
        async def refused(configuration):
            raise WriterUnsupported(PublicDiagnostic(DiagnosticCode.IGNORE_STRING))
        monkeypatch.setattr(b, 'validate_selection_configuration', refused)
        with pytest.raises(WriterUnsupported):
            await b.refresh(force=True)
        assert (b.sync_max_size_mb, b.text_scan_max_bytes, b.selection_use_ignore_files,
                b.selection_ignore_files, b._recommendation_policy) == old
    finally:
        await b.close()


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_live_helper_size_expectation(tmp_path, kind, monkeypatch):
    from scripts import live_check_parity as helper
    monkeypatch.setattr(helper, 'MIB', 8)
    async with vault(tmp_path / 'state', kind, live()) as (b, writes):
        await b.startup_check()
        root = tmp_path / 'vault'
        folder = root / helper.PREFIX
        folder.mkdir(parents=True)
        for name, size in [('device-40m.bin', 40*8), ('exact.bin', 100*8)]:
            data = b'x' * size
            (folder / name).write_bytes(data)
            await b.write_attachment(helper.PREFIX + '/' + name, data, create=True)
        (folder / 'above.bin').write_bytes(b'x' * (100*8+1))
        good, report = await helper.verify_size(b, root, 100)
        assert good
        assert report['recommendation_policy']['syncMaxSizeInMB']['source'] == 'device'


async def test_refresh_selection_and_provenance_follow_devices(tmp_path):
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        for use, names in [(True, 'first'), (True, 'second'), (False, 'third')]:
            m = live()
            m['tweak_values']['PREFERRED'].update(useIgnoreFiles=False, ignoreFiles='stale')
            m['tweak_values']['private-device-id'].update(useIgnoreFiles=use, ignoreFiles=names)
            storage._objects[MILESTONE_KEY] = json.dumps(m).encode()
            await b.refresh(force=True)
            assert b.selection_use_ignore_files is use
            assert b.selection_ignore_files == (names,)
            policy = (await b.backend_info())['recommendation_policy']
            assert policy['useIgnoreFiles'] == dict(value=use, source='device', device_count=1, disagree=False)
            assert policy['ignoreFiles'] == dict(name_count=1, source='device', device_count=1, disagree=False)
    finally:
        await b.close()


def test_inactive_name_ambiguity_still_names_override():
    # P-48 arbitrates all three keys independently; inactivity only avoids
    # type-validation and file reads (C:targetFilter.ts:134-136).
    m = {'tweak_values': {'a': {'useIgnoreFiles': False, 'ignoreFiles': 'a'},
                         'b': {'useIgnoreFiles': False, 'ignoreFiles': 'b'}}}
    with pytest.raises(fmt.UnsupportedVaultSettings, match='LIVESYNC_IGNORE_FILES'):
        fmt.resolve_ignore_settings(m)
    assert fmt.resolve_ignore_settings(m, names_override=('chosen',)) == (False, ('chosen',))


def test_override_metadata_retains_negative_unlimited():
    from diagnostic_hygiene import recommendation_fields
    policies = fmt.resolve_writer_policy(live(), size_override=-1,
        use_override=False, names_override=('private',))
    projected = recommendation_fields(fmt.writer_policy_summary(policies))
    # The override keeps its meaning inside the peer; the published value
    # stays a non-negative number or null.
    assert policies[0].value == -1
    assert projected['syncMaxSizeInMB'] == dict(value=None, source='override', device_count=1, disagree=False)
    assert projected['useIgnoreFiles']['source'] == 'override'
    assert projected['ignoreFiles']['source'] == 'override'


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
@pytest.mark.parametrize('override', [-1, 0, 60])
async def test_size_overrides_cross_the_same_boundary(tmp_path, kind, override, monkeypatch):
    import server
    m = live()
    m['tweak_values']['private-device-id']['syncMaxSizeInMB'] = {'toString': 5}
    async with vault(tmp_path, kind, m) as (b, writes):
        b.sync_max_size_mb_override = override
        await b.startup_check()
        assert b.sync_max_size_mb == override
        want = {60: 60, 0: 0, -1: None}[override]
        policy = (await b.backend_info())['recommendation_policy']
        assert policy['syncMaxSizeInMB'] == dict(value=want, source='override', device_count=1, disagree=False)
        monkeypatch.setattr(server, 'backend', b)
        stats = json.loads(await server.obsidian_vault_stats(), parse_constant=lambda token: pytest.fail(token))
        assert stats['recommendation_policy'] == policy
        assert not writes()


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
@pytest.mark.parametrize('preferred', [False, True], ids=['without-preferred', 'with-preferred'])
@pytest.mark.parametrize('key,old,new,env', KEYS)
async def test_agreeing_newest_devices_override_older_disagreement(tmp_path, kind, preferred, key, old, new, env):
    """P-48a peer arbitration, not an invented plugin multi-device oracle.

    Native numeric newer-side rule: P:ModuleResolveMismatchedTweaks.ts:120-132.
    Recommendation-only keys: C:common/models/tweak.definition.ts:116-135.
    Catch treating any newest tie as ambiguous or falling back to older/PREFERRED.
    """
    common = {'pathNormalizationVersion': 0, 'useIgnoreFiles': True}
    entries = {
        'older': {**common, key: old, 'tweakModified': 10},
        'newest-a': {**common, key: new, 'tweakModified': 20},
        'newest-b': {**common, key: new, 'tweakModified': 20},
    }
    if preferred:
        entries['PREFERRED'] = {**common, key: old, 'tweakModified': 30}
    async with vault(tmp_path, kind, {'tweak_values': entries}) as (backend, writes):
        await backend.startup_check()
        effective = {'syncMaxSizeInMB': backend.sync_max_size_mb,
                     'useIgnoreFiles': backend.selection_use_ignore_files,
                     'ignoreFiles': backend.selection_ignore_files}
        assert effective[key] == ((new,) if key == 'ignoreFiles' else new)
        info = (await backend.backend_info())['recommendation_policy'][key]
        assert info['source'] == 'device'
        assert info['device_count'] == 3 and info['disagree'] is True
        assert not writes()


@pytest.mark.parametrize('key,old,new,env', KEYS)
def test_summary_claims_match_resolution(key, old, new, env):
    # The order that the README and the three short policy sections state.
    base = {'pathNormalizationVersion': 0}
    default = {'syncMaxSizeInMB': 50, 'useIgnoreFiles': False, 'ignoreFiles': '.gitignore'}[key]
    entries = {'a': {**base, key: new}, 'b': {**base, key: new}}
    assert resolve({'tweak_values': entries}, key) == new  # agreement needs no timestamp
    entries['c'] = {**base, key: old, 'tweakModified': 5}
    assert resolve({'tweak_values': entries}, key) == old  # a disagreement goes to the newest
    entries.pop('c')
    entries['d'] = {**base, key: old}
    with pytest.raises(fmt.UnsupportedVaultSettings, match=env):  # unresolved without PREFERRED
        resolve({'tweak_values': entries}, key)
    entries['PREFERRED'] = {**base, key: old}
    assert resolve({'tweak_values': entries}, key) == old  # PREFERRED decides the disagreement
    unadvertised = {'tweak_values': {'PREFERRED': {**base, key: old}, 'x': dict(base)}}
    assert resolve(unadvertised, key) == old  # nobody advertises: PREFERRED
    assert resolve({'tweak_values': {'x': dict(base)}}, key) == default  # then the device default
