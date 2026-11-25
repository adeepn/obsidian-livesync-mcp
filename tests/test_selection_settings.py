"""G1: C:tweak.definition.ts:116-149; defaults:140-141; targetFilter.ts:95,205.
Incoming reflection: C:serviceModules/ServiceFileHandlerBase/events.ts:217-219.
"""
import json
import pytest
import livesync_format as fmt
from storage_base import SelectionRefused, UnsupportedVault, StorageUnavailable
from tests.test_tweak_startup import vault
from tests.test_storage_journal import make_backend, healthy_bucket
from storage_journal import MILESTONE_KEY


def milestone(**settings):
    return {'tweak_values': {'PREFERRED': {'pathNormalizationVersion': 0, **settings}}}


@pytest.mark.parametrize('use', [None, False, True])
@pytest.mark.parametrize('names', [None, ('override',)])
def test_independent_overrides(use, names):
    assert fmt.resolve_ignore_settings(milestone(useIgnoreFiles=True, ignoreFiles=' a, b '),
        use_override=use, names_override=names) == (
            True if use is None else use, ('a', 'b') if names is None else names)


@pytest.mark.parametrize('value', [True, False])
def test_boolean_resolution(value):
    assert fmt.resolve_ignore_settings(milestone(useIgnoreFiles=value)) == (value, ('.gitignore',))


def test_absent_defaults_and_preferred():
    assert fmt.resolve_ignore_settings(milestone()) == (False, ('.gitignore',))
    m = milestone(useIgnoreFiles=True)
    m['tweak_values']['device'] = {'useIgnoreFiles': False}
    assert fmt.resolve_ignore_settings(m) == (True, ('.gitignore',))


@pytest.mark.parametrize('key,value,message', [
    ('useIgnoreFiles', 1, 'useIgnoreFiles must be a boolean'),
    ('useIgnoreFiles', 'secret', 'useIgnoreFiles must be a boolean'),
    ('ignoreFiles', False, 'ignoreFiles must be a string'),
])
def test_invalid_settings(key, value, message):
    with pytest.raises(fmt.UnsupportedVaultSettings, match=message):
        fmt.resolve_ignore_settings(milestone(useIgnoreFiles=True, **{key: value}) if key != 'useIgnoreFiles' else milestone(**{key: value}))


@pytest.mark.parametrize('key,a,b,env', [('useIgnoreFiles', True, False, 'LIVESYNC_USE_IGNORE_FILES'),
    ('ignoreFiles', 'a', 'b', 'LIVESYNC_IGNORE_FILES')])
def test_ambiguous_maps(key, a, b, env):
    m = {'tweak_values': {'a': {'useIgnoreFiles':True, key: a}, 'b': {'useIgnoreFiles':True, key: b}}}
    with pytest.raises(fmt.UnsupportedVaultSettings, match=env):
        fmt.resolve_ignore_settings(m)


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_inherited_policy_preserves_storage(tmp_path, kind):
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True)) as (b, writes):
        await b.startup_check()
        # Ordinary hidden file stored through existing raw-backend helper.
        await seed_hidden_ignore(b, 'private/**')
        before = len(writes())
        with pytest.raises(SelectionRefused):
            await b.write_note('private/no.md', 'no', create=True)
        assert len(writes()) == before
        await b.write_note('ok.md', 'yes', create=True)
        assert (await b.read_note('ok.md'))['text'] == 'yes'
        assert (await b.backend_info())['selection_use_ignore_files_source'] == 'vault'


async def test_journal_refresh_rechecks_policy(tmp_path):
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        await b.write_note('private/no.md', 'old', create=True)
        await seed_hidden_ignore(b, 'private/**')
        storage._objects[MILESTONE_KEY] = json.dumps(milestone(useIgnoreFiles=True)).encode()
        await b.refresh(force=True)
        before = len(storage.put_calls)
        with pytest.raises(SelectionRefused):
            await b.write_note('private/no.md', 'new')
        assert len(storage.put_calls) == before
        assert (await b.read_note('private/no.md'))['text'] == 'old'
    finally:
        await b.close()


async def seed_hidden_ignore(b, text):
    import base64
    encoded = base64.b64encode(text.encode()).decode()
    chunk = fmt.chunk_id_for(encoded)
    doc = dict(_id='i:.gitignore', path='i:.gitignore', type='newnote', ctime=1, mtime=1,
               size=len(text), children=[chunk], eden={chunk: encoded})
    if hasattr(b, '_put_doc'):
        await b._put_doc(doc['_id'], doc)
    else:
        doc['_rev'] = '1-ignore'
        b._store.apply_journal('seed-ignore', {}, [doc])


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
@pytest.mark.parametrize('key,a,b,variable', [
    ('useIgnoreFiles', True, False, 'LIVESYNC_USE_IGNORE_FILES'),
    ('ignoreFiles', 'a', 'b', 'LIVESYNC_IGNORE_FILES')])
async def test_backend_disagreement_refuses_before_mutation(tmp_path, kind, key, a, b, variable):
    async with vault(tmp_path, kind, {'tweak_values': {'a': {'useIgnoreFiles':True,key:a}, 'b': {'useIgnoreFiles':True,key:b}}}) as (b, writes):
        with pytest.raises(UnsupportedVault, match=variable):
            await b.startup_check()
        assert not writes()


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_explicit_false_allows_ignored_path(tmp_path, kind):
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True)) as (b, writes):
        b.selection_use_ignore_files = False
        await b.startup_check()
        await seed_hidden_ignore(b, '**/*.md')
        await b.write_note('private.md', 'yes', create=True)
        assert (await b.read_note('private.md'))['text'] == 'yes'
        info = await b.backend_info()
        assert info['selection_use_ignore_files_source'] == 'explicit'
        assert info['selection_ignore_files_source'] == 'vault'
        assert info['selection_engine_state'] == 'off'


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_inherited_ignore_requires_node(tmp_path, kind, monkeypatch):
    import selection_engine
    def missing(*a, **kw):
        raise FileNotFoundError('private remote value')
    monkeypatch.setattr(selection_engine.subprocess, 'Popen', missing)
    from storage_base import WriterUnsupported
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True)) as (b, writes):
        with pytest.raises(WriterUnsupported, match='install Node.js 22') as error:
            await b.startup_check()
        assert 'private remote value' not in str(error.value)
        assert not writes()


def test_upstream_template_and_name_vectors():
    from tests.test_tweak_differential import FIXTURE
    oracle = FIXTURE['selection']
    keys = set(oracle['templateKeys'])
    assert {'useIgnoreFiles','ignoreFiles'} <= keys
    assert not {'syncOnlyRegEx','syncIgnoreRegEx'} & keys
    defaults = oracle['defaults']
    assert fmt.resolve_ignore_settings(milestone()) == (
        defaults['useIgnoreFiles'], (defaults['ignoreFiles'],))
    for case in oracle['names']:
        assert fmt.resolve_ignore_settings(milestone(ignoreFiles=case['input']))[1] == tuple(case['expected'])


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_selection_sources_reach_stats(tmp_path, kind, monkeypatch):
    import server
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True)) as (b, writes):
        await b.startup_check()
        monkeypatch.setattr(server, 'backend', b)
        info = json.loads(await server.obsidian_vault_stats())
        assert info['selection_use_ignore_files_source'] == 'vault'
        assert info['selection_ignore_files_source'] == 'vault'
        assert info['selection_only_source'] == 'explicit'
        assert info['selection_ignore_source'] == 'explicit'
        assert info['selection_engine_state'] == 'ready'
        assert info['filters_active'] is True


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_changed_names_revalidate_and_invalidate_verdicts(tmp_path, kind):
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True, ignoreFiles='missing')) as (b, writes):
        await b.startup_check()
        await seed_hidden_ignore(b, 'private/**')
        assert (await b.selection_for('private/no.md')).allowed
        rules = b._selection_rules
        evaluations = b._selection_worker.evaluations
        # Use the same path and ignore-file bytes with a newly effective config.
        b.resolve_selection_settings(milestone(useIgnoreFiles=True, ignoreFiles='.gitignore'))
        before = len(writes())
        with pytest.raises(SelectionRefused):
            await b.write_note('private/no.md', 'no', create=True)
        assert len(writes()) == before
        assert b._selection_rules != rules
        assert b._selection_checked_configuration == b._selection_configuration()
        assert b._selection_worker.evaluations >= evaluations + 2  # startup and verdict
        evaluations = b._selection_worker.evaluations
        assert not (await b.selection_for('private/no.md')).allowed
        assert b._selection_worker.evaluations == evaluations
        if kind == 's3':
            cached = b._store._conn.execute('SELECT DISTINCT rules FROM selection_verdicts').fetchall()
            assert [row[0] for row in cached] == [b._selection_rules]


async def test_journal_refresh_flips_cached_active_policy(tmp_path):
    b, storage = make_backend(tmp_path, healthy_bucket())
    b.selection_only = '.*'
    try:
        await b.startup_check()
        await seed_hidden_ignore(b, 'private/**')
        assert (await b.selection_for('private/no.md')).allowed
        old_rules = b._selection_rules
        storage._objects[MILESTONE_KEY] = json.dumps(milestone(useIgnoreFiles=True)).encode()
        await b.refresh(force=True)
        assert not (await b.selection_for('private/no.md')).allowed
        assert b._selection_rules != old_rules
        storage._objects[MILESTONE_KEY] = json.dumps(milestone(useIgnoreFiles=False)).encode()
        await b.refresh(force=True)
        assert (await b.selection_for('private/no.md')).allowed
        assert b._selection_rules == old_rules
    finally:
        await b.close()


async def test_failed_refresh_blocks_writes_until_policy_repaired(tmp_path):
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        original = storage._objects[MILESTONE_KEY]
        storage._objects[MILESTONE_KEY] = json.dumps(milestone(useIgnoreFiles='private marker')).encode()
        with pytest.raises(UnsupportedVault, match='useIgnoreFiles must be a boolean'):
            await b.refresh(force=True)
        before = len(storage.put_calls)
        with pytest.raises(UnsupportedVault, match='useIgnoreFiles must be a boolean'):
            await b.write_note('no.md', 'no', create=True)
        assert len(storage.put_calls) == before
        storage._objects[MILESTONE_KEY] = original
        await b.refresh(force=True)
        await b.write_note('yes.md', 'yes', create=True)
    finally:
        await b.close()


async def test_stats_selection_fields_are_closed(monkeypatch):
    import server
    from tests.fake_backend import FakeBackend
    b = FakeBackend()
    async def info():
        return dict(backend='fake', selection_use_ignore_files_source='private marker',
                    selection_ignore_files_source={'raw': 'private marker'},
                    selection_engine_state='private marker', filters_active='private marker',
                    selection_writer_supported=['private marker'])
    monkeypatch.setattr(b, 'backend_info', info)
    monkeypatch.setattr(server, 'backend', b)
    result = json.loads(await server.obsidian_vault_stats())
    assert 'private marker' not in json.dumps(result)
    assert result['selection_engine_state'] == 'unsupported'
    assert result['filters_active'] is False
    assert result['selection_writer_supported'] is False


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
async def test_reads_bypass_inherited_and_explicit_filters(tmp_path, kind):
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True)) as (b, writes):
        await b.startup_check()
        await b.write_note('later-ignored.md', 'retained', create=True)
        await seed_hidden_ignore(b, '**/*.md')
        assert (await b.read_note('later-ignored.md'))['text'] == 'retained'
        b.selection_only = 'never-match'
        b.selection_ignore = 'later-ignored'
        assert (await b.read_note('later-ignored.md'))['text'] == 'retained'


@pytest.mark.parametrize('kind', ['couchdb', 's3'])
@pytest.mark.parametrize('error', [ValueError, StorageUnavailable])
async def test_unreadable_ignore_file_matches_upstream(tmp_path, kind, error, monkeypatch):
    # C:serviceFeatures/targetFilter.ts:102-127 treats unreadable files as absent.
    from tests.test_tweak_differential import FIXTURE
    async with vault(tmp_path, kind, milestone(useIgnoreFiles=True)) as (b, writes):
        await b.startup_check()
        async def unreadable(path):
            raise error('synthetic read failure')
        monkeypatch.setattr(b, '_read_selection_file', unreadable)
        assert await b._selection_lines('.gitignore') is FIXTURE['selection']['unreadable']
        await b.write_note('admitted.md', 'retained', create=True)
        assert (await b.read_note('admitted.md'))['text'] == 'retained'


IGNORE_NAMES_ORACLE = json.loads(__import__('pathlib').Path('tests/fixtures/ignore_names_upstream.json').read_text())

@pytest.mark.parametrize('kind',['couchdb','s3'])
@pytest.mark.parametrize('explicit',[False,True])
@pytest.mark.parametrize('row',IGNORE_NAMES_ORACLE['rows'])
async def test_device_ignore_names_lookup(tmp_path,kind,explicit,row,monkeypatch):
    from config import load_config
    m=milestone(**row['settings'])
    async with vault(tmp_path,kind,m) as (b,writes):
        if explicit:
            # Exercise the real operator parser, including JS trim and empty entries.
            env={'LIVESYNC_BACKEND':'couchdb','COUCHDB_USER':'test-user','COUCHDB_PASSWORD':'test-password','COUCHDB_DATABASE':'vault','MCP_API_KEY':'test-key', 'LIVESYNC_IGNORE_FILES':row['settings']['ignoreFiles']}
            b.selection_ignore_files=load_config(env).selection_ignore_files
        lookups=[]
        async def read(path):
            lookups.append(path)
            if row['mode']=='unreadable':raise StorageUnavailable('unreadable')
            return None if row['mode']=='missing' else '*.md\n!keep.md'
        monkeypatch.setattr(b,'_read_selection_file',read)
        await b.startup_check()
        assert b.selection_ignore_files==tuple(row['names'])
        assert (await b.selection_for('note.md')).allowed is row['allowed']
        if not row['settings']['useIgnoreFiles']:assert not lookups
        info=await b.backend_info()
        expected=len(row['names']) if explicit and row['settings']['ignoreFiles'] and row['settings']['useIgnoreFiles'] and row['mode']!='readable' else 0
        assert type(info['selection_unresolved_ignore_names']) is int
        assert info['selection_unresolved_ignore_names']==expected

@pytest.mark.parametrize('kind',['couchdb','s3'])
async def test_ignore_lookup_never_reads_service_or_escaping_path(tmp_path,kind,monkeypatch):
    async with vault(tmp_path,kind,milestone(useIgnoreFiles=True,ignoreFiles='../escape,h:leaf,_local/secrets')) as (b,writes):
        await b.startup_check()
        assert (await b.selection_for('note.md')).allowed
        # The storage-specific readers enforce namespace safety even if called directly.
        if kind=='couchdb':
            async def forbidden_read(*args,**kwargs):
                pytest.fail('unusable ignore name reached the document transport')
            monkeypatch.setattr(b,'_get_doc',forbidden_read)
        else:
            def forbidden_read(*args,**kwargs):
                pytest.fail('unusable ignore name reached note-state lookup')
            monkeypatch.setattr(b._store,'winner',forbidden_read)
        for path in ('../escape','h:leaf','_local/secrets','f:folder',''):
            assert await b._read_selection_file(path) is None

@pytest.mark.parametrize('kind',['couchdb','s3'])
async def test_explicit_warning_tracks_current_lookup_and_recovers(tmp_path,kind,monkeypatch):
    async with vault(tmp_path,kind,milestone(useIgnoreFiles=True)) as (b,writes):
        b.selection_ignore_files=('rules',)
        state=['*.md']
        async def read(path):
            if state[0]=='unreadable':raise StorageUnavailable('unreadable')
            return state[0]
        monkeypatch.setattr(b,'_read_selection_file',read)
        await b.startup_check()
        for text,expected in [('*.md',0),(None,1),('unreadable',1),('*.md',0)]:
            state[0]=text
            await b.selection_for('note.md')
            assert b.selection_info()['selection_unresolved_ignore_names']==expected
            import server
            monkeypatch.setattr(server,'backend',b)
            stats=json.loads(await server.obsidian_vault_stats())
            assert type(stats['selection_unresolved_ignore_names']) is int
            assert stats['selection_unresolved_ignore_names']==expected

@pytest.mark.parametrize('value,expected',[(7,7),(True,0),(-1,0),('PRIVATE_WARNING',0),({'raw':'PRIVATE_WARNING'},0),(1.5,0)])
async def test_stats_ignore_warning_count_is_typed(monkeypatch,value,expected):
    import server
    from tests.fake_backend import FakeBackend
    b=FakeBackend()
    async def info():return {'backend':'fake','selection_unresolved_ignore_names':value}
    monkeypatch.setattr(b,'backend_info',info)
    monkeypatch.setattr(server,'backend',b)
    result=json.loads(await server.obsidian_vault_stats())
    assert result['selection_unresolved_ignore_names']==expected
    assert type(result['selection_unresolved_ignore_names']) is int
    assert 'PRIVATE_WARNING' not in str(result)


async def test_failed_policy_refresh_preserves_effective_tuple(tmp_path):
    # VaultService.ts:73-81 and targetFilter.ts:132-137 consume one realised
    # settings snapshot. Installing selection before validating size is unsafe.
    b, storage = make_backend(tmp_path, healthy_bucket())
    try:
        await b.startup_check()
        before = (b.selection_use_ignore_files, b.selection_ignore_files,
                  b.sync_max_size_mb, b.text_scan_max_bytes)
        storage._objects[MILESTONE_KEY] = json.dumps(milestone(
            useIgnoreFiles=True, ignoreFiles='changed', syncMaxSizeInMB='INVALID')).encode()
        with pytest.raises(UnsupportedVault):
            await b.refresh(force=True)
        assert (b.selection_use_ignore_files, b.selection_ignore_files,
                b.sync_max_size_mb, b.text_scan_max_bytes) == before
        assert b._selection_resolution_diagnostic is not None
    finally:
        await b.close()
