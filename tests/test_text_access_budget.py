"""One text access policy; device size predicate: C:services/base/VaultService.ts:73-81."""
import base64
import json
import pytest
import server
from config import load_config
from tests.test_config import COUCHDB_ENV
from tests.test_upload_attachment import backend


@pytest.mark.parametrize('suffix', ['.md', '.json'])
async def test_boundary_and_unlimited(backend, monkeypatch, suffix):
    monkeypatch.setattr(server, 'backend', backend)
    path = 'budget' + suffix
    backend.text_scan_max_bytes = load_config({**COUCHDB_ENV,
        'LIVESYNC_TEXT_SCAN_MAX_BYTES': '4'}).text_scan_max_bytes
    await backend.write_note(path, 'éé', create=True)
    assert json.loads(await server.obsidian_fetch(path))['content'] == 'éé'
    assert (await backend.read_text_note(path))['text'] == 'éé'
    await backend.write_note(path, 'ééx')
    result = json.loads(await server.obsidian_fetch(path))
    assert result['content_available'] is False
    assert result['size_bytes'] == 5
    assert result['budget_bytes'] == 4
    assert result['reason'] == 'text_budget_exceeded'
    assert 'LIVESYNC_TEXT_SCAN_MAX_BYTES' in result['remedy']
    from storage_base import TextBudgetExceeded
    with pytest.raises(TextBudgetExceeded) as caught:
        await backend.read_text_note(path)
    assert caught.value.as_dict()['size_bytes'] == 5
    response = await server.obsidian_update_note(path, 'append', content='SECRET')
    assert 'LIVESYNC_TEXT_SCAN_MAX_BYTES' in response
    assert 'ééx' not in response and 'SECRET' not in response
    assert base64.b64decode(json.loads(await server.obsidian_download_attachment(path))['content_b64']) == 'ééx'.encode()
    entries = [text async for meta, text in backend.iterate_texts() if meta["path"] == path]
    assert entries == [None]
    backend.text_scan_max_bytes = load_config({**COUCHDB_ENV,
        'LIVESYNC_TEXT_SCAN_MAX_BYTES': '0'}).text_scan_max_bytes
    assert json.loads(await server.obsidian_fetch(path))['content'] == 'ééx'
    assert (await backend.read_text_note(path))['text'] == 'ééx'
    assert [text async for meta, text in backend.iterate_texts() if meta["path"] == path] == ['ééx']
    assert 'Error' not in await server.obsidian_update_note(path, 'append', content='ok')


async def test_policy_resolution_and_override(backend):
    backend.text_scan_max_bytes = None
    assert backend.text_scan_max_bytes == 256 * 1024 * 1024
    backend.sync_max_size_mb = 300.25
    assert backend.text_scan_max_bytes == int(300.25 * 1024 * 1024)
    backend.text_scan_max_bytes = 7
    backend.sync_max_size_mb = 900
    assert backend.text_scan_max_bytes == 7
    backend.text_scan_max_bytes = 0
    assert backend.text_scan_max_bytes == 0


def test_configuration():
    cfg = load_config(COUCHDB_ENV)
    assert cfg.text_scan_max_bytes is None
    assert cfg.regex_max_output_bytes == 0
    assert cfg.regex_timeout_seconds == 10
    assert load_config({**COUCHDB_ENV, 'LIVESYNC_TEXT_SCAN_MAX_BYTES': '0'}).text_scan_max_bytes == 0


def test_regex_limits():
    import regex_runner
    with pytest.raises(regex_runner.RegexOutputExceeded, match='LIVESYNC_REGEX_MAX_OUTPUT_BYTES'):
        regex_runner.apply('x', 'x', 'subn', replacement='secret', max_output_bytes=5)
    assert regex_runner.apply('x', 'x', 'subn', replacement='secret', max_output_bytes=6)['result'] == 'secret'
    with pytest.raises(regex_runner.RegexTimeoutError, match='LIVESYNC_REGEX_TIMEOUT_SECONDS'):
        regex_runner.apply('(a+)+$', 'a' * 1000 + '!', 'subn', .1)


@pytest.mark.parametrize('mode,kwargs', [
    ('overwrite', {'content': 'safe'}), ('append', {'content': 'safe'}),
    ('prepend', {'content': 'safe'}), ('after-heading', {'heading': 'h', 'content': 'safe'}),
    ('search-replace', {'search': 'secret', 'replace': 'safe'}),
    ('search-replace', {'search': 'secret', 'replace': 'safe', 'regex': True}),
])
@pytest.mark.parametrize('suffix', ['.md', '.json'])
async def test_all_edit_modes_refuse_without_body_leak(backend, monkeypatch, mode, kwargs, suffix):
    monkeypatch.setattr(server, 'backend', backend)
    backend.text_scan_max_bytes = 4
    path = 'edit' + suffix
    await backend.write_note(path, 'secret', create=True)
    before = await backend.read_note(path)
    response = json.loads(await server.obsidian_update_note(path, mode, **kwargs))
    assert response['reason'] == 'text_budget_exceeded'
    assert response['budget_bytes'] == 4
    assert 'secret' not in str(response)
    assert (await backend.read_note(path))['rev'] == before['rev']


async def test_other_text_tools_and_stats(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    backend.text_scan_max_bytes = 3
    backend.regex_max_output_bytes = 9
    backend.regex_timeout_seconds = .75
    await backend.write_note('props.md', '# h\nsecret', create=True)
    for call in [server.obsidian_manage_properties('props.md'),
                 server.obsidian_manage_tags('props.md')]:
        result = json.loads(await call)
        assert result['reason'] == 'text_budget_exceeded'
        assert 'secret' not in str(result)
    stats = json.loads(await server.obsidian_vault_stats())
    assert stats['text_budget_bytes'] == 3
    assert stats['regex_max_output_bytes'] == 9
    assert stats['regex_timeout_seconds'] == .75


@pytest.mark.parametrize('key,value', [
    ('LIVESYNC_TEXT_SCAN_MAX_BYTES', '-1'),
    ('LIVESYNC_REGEX_MAX_OUTPUT_BYTES', '-1'),
    ('LIVESYNC_REGEX_MAX_OUTPUT_BYTES', '1.5'),
    ('LIVESYNC_REGEX_TIMEOUT_SECONDS', 'nan'),
    ('LIVESYNC_REGEX_TIMEOUT_SECONDS', 'inf'),
    ('LIVESYNC_REGEX_TIMEOUT_SECONDS', '0'),
    ('LIVESYNC_REGEX_TIMEOUT_SECONDS', '-1'),
])
def test_invalid_limits_are_secret_safe(key, value):
    from config import ConfigError
    with pytest.raises(ConfigError, match=key) as caught:
        load_config({**COUCHDB_ENV, key: value})
    assert value not in str(caught.value).replace(key, '')


def upstream_fixture():
    from pathlib import Path
    return json.loads((Path(__file__).parent / 'fixtures/text_budget_upstream.json').read_text())


async def test_size_policy_uses_executed_upstream_float_product(backend):
    # Executed C:services/base/VaultService.ts:73-81, fixture records source SHA.
    from storage_base import check_file_size
    from storage_base import WriteSkipped
    for row in upstream_fixture()['policy']:
        if row['too_large']:
            with pytest.raises(WriteSkipped):
                check_file_size('probe.md', row['size'], row['mb'])
        else:
            check_file_size('probe.md', row['size'], row['mb'])
        backend.text_scan_max_bytes = None
        backend.sync_max_size_mb = row['mb']
        if row['mb'] > 256 and backend.text_scan_max_bytes:
            assert (row['size'] > backend.text_scan_max_bytes) == row['too_large']


async def test_twelve_mib_newnote_whole_text_and_oracle_children(backend, monkeypatch):
    # C:ContentSplitter/ContentSplitters.ts:32-36 and ContentSplitterV2.ts:18-31;
    # C:managers/HashManager/XXHashHashManager.ts:97-98 (published WASM).
    import hashlib
    from content_splitter import ContentSplitter
    monkeypatch.setattr(server, 'backend', backend)
    vector = upstream_fixture()['large']
    backend._splitter = ContentSplitter(vector['settings'])
    text = 'a' * (12 * 1024 * 1024 - 6) + 'needle'
    path = 'large.json'
    await backend.write_note(path, text, create=True)
    doc = (backend._store.winner(path) if hasattr(backend, '_store')
           else await backend._get_doc(path))
    assert doc['children'] == vector['children']
    if hasattr(backend, '_store'):
        pieces = backend._store._chunk_datas(doc['children'], {}, path)
    else:
        pieces = await backend._fetch_chunk_datas(doc['children'], path, doc.get('eden'))
    assert [hashlib.sha256(piece.encode()).hexdigest() for piece in pieces] == vector['piece_sha256']
    assert json.loads(await server.obsidian_fetch(path))['content'] == text
    assert path in [r['path'] for r in json.loads(await server.obsidian_search('needle'))['results']]
    assert 'Error' not in await server.obsidian_update_note(path, 'search-replace', search='needle', replace='NEEDLE', regex=True)
    assert (await backend.read_text_note(path))['text'] == text[:-6] + 'NEEDLE'
    assert 'Error' not in await server.obsidian_update_note(path, 'append', content='tail')
    assert (await backend.read_text_note(path))['text'].endswith('NEEDLE\ntail')
    # Existing properties/tags restrictions remain .md-only, even for text newnotes.
    assert 'properties require a .md file' in await server.obsidian_manage_properties(path, 'set', properties='{"large": true}')
    assert 'tags require a .md file' in await server.obsidian_manage_tags(path, 'add', tags='large')
    assert 'Error' not in await server.obsidian_update_note(path, 'overwrite', content=text)
    assert (await backend.read_text_note(path))['text'] == text
    # Disk suffix matching is case-sensitive (C:string_and_binary/path.ts:164-183),
    # so .MD is a newnote while the existing properties/tags tools accept it.
    await backend.copy_note(path, 'large.MD')
    assert (await backend.read_note('large.MD'))['meta']['type'] == 'newnote'
    assert 'Error' not in await server.obsidian_manage_properties('large.MD', 'set', properties='{"large": true}')
    assert json.loads(await server.obsidian_manage_properties('large.MD'))['large'] is True
    assert 'Error' not in await server.obsidian_manage_tags('large.MD', 'add', tags='large')
    assert 'large' in await server.obsidian_manage_tags('large.MD')
    assert (await backend.read_text_note('large.MD'))['text'].endswith(text)


async def test_sixty_mib_plain_note_journal(backend, monkeypatch):
    if not hasattr(backend, '_store'):
        return  # Real-size plain case is required on the journal double only.
    monkeypatch.setattr(server, 'backend', backend)
    # Device admission must allow this file; the default text access budget
    # stays 256 MiB. C:services/base/VaultService.ts:73-81 rejects >50 MiB.
    backend.sync_max_size_mb = 64
    text = 'a' * (60 * 1024 * 1024 - 6) + 'needle'
    await backend.write_note('large.md', text, create=True)
    assert json.loads(await server.obsidian_fetch('large.md', include_properties=False))['content'] == text
    assert 'large.md' in [r['path'] for r in json.loads(await server.obsidian_search('needle'))['results']]
    assert 'Error' not in await server.obsidian_update_note('large.md', 'search-replace', search='needle', replace='NEEDLE')
    assert (await backend.read_text_note('large.md'))['text'] == text[:-6] + 'NEEDLE'


async def test_reresolution_tracks_vault_policy(tmp_path):
    # C:services/base/VaultService.ts:73-81; startup reuses resolved PREFERRED.
    from tests.test_tweak_startup import vault
    for kind in ('couchdb', 'journal'):
        async with vault(tmp_path / kind, kind, {'tweak_values': {'PREFERRED': {
            'pathNormalizationVersion': 0, 'syncMaxSizeInMB': 300.25}}}) as (b, _):
            await b.startup_check()
            assert b.text_scan_max_bytes == int(300.25 * 1024 * 1024)
            async def change_vault_size(value):
                if kind == 'couchdb':
                    key = '_local/obsydian_livesync_milestone'
                    doc = await b._get_doc(key)
                    doc['tweak_values']['PREFERRED']['syncMaxSizeInMB'] = value
                    await b._put_doc(key, doc)
                else:
                    from storage_journal import MILESTONE_KEY
                    doc = json.loads(b._object_storage._objects[MILESTONE_KEY])
                    doc['tweak_values']['PREFERRED']['syncMaxSizeInMB'] = value
                    b._object_storage._objects[MILESTONE_KEY] = json.dumps(doc).encode()
            await change_vault_size(350.5)
            await b.startup_check()
            assert b.text_scan_max_bytes == int(350.5 * 1024 * 1024)
            b.text_scan_max_bytes = 1
            await change_vault_size(400)
            await b.startup_check()
            assert b.text_scan_max_bytes == 1


def test_budget_error_rejects_untyped_fields():
    from storage_base import TextBudgetExceeded
    class Hostile:
        def __str__(self):
            raise AssertionError('must never stringify')
        def __int__(self):
            raise AssertionError('must never coerce')
    result = TextBudgetExceeded(Hostile(), 'PRIVATE').as_dict()
    assert result['size_bytes'] is None and result['budget_bytes'] is None
    assert 'PRIVATE' not in str(result)


def test_regex_default_accepts_twelve_mib():
    import regex_runner
    text = 'a' * (12 * 1024 * 1024)
    assert regex_runner.apply('z', text, 'subn')['result'] == text


async def test_large_finite_budget_has_no_sqlite_ceiling(backend):
    backend.text_scan_max_bytes = 2 ** 100
    await backend.write_note('binary.base', b'\x00', create=True)
    assert (await backend.read_text_note('binary.base'))['text'] is None
    assert (await backend.read_text_note('binary.base'))['text'] is None


async def test_regex_tool_refusals_are_distinct_and_preserve_revision(backend, monkeypatch):
    monkeypatch.setattr(server, 'backend', backend)
    await backend.write_note('regex.md', 'a' * 1000 + '!', create=True)
    rev = (await backend.read_note('regex.md'))['rev']
    backend.regex_max_output_bytes = 5
    size = await server.obsidian_update_note('regex.md', 'search-replace',
        search='a+', replace='PRIVATE_OUTPUT', regex=True)
    assert 'LIVESYNC_REGEX_MAX_OUTPUT_BYTES' in size
    assert 'LIVESYNC_REGEX_TIMEOUT_SECONDS' not in size
    assert 'PRIVATE_OUTPUT' not in size
    backend.regex_timeout_seconds = .1
    timeout = await server.obsidian_update_note('regex.md', 'search-replace',
        search='(a+)+$', replace='PRIVATE_OUTPUT', regex=True)
    assert 'LIVESYNC_REGEX_TIMEOUT_SECONDS' in timeout
    assert 'LIVESYNC_REGEX_MAX_OUTPUT_BYTES' not in timeout
    assert 'PRIVATE_OUTPUT' not in timeout
    assert (await backend.read_note('regex.md'))['rev'] == rev


@pytest.mark.parametrize('size_override',[None,0,400.5])
@pytest.mark.parametrize('text_override',[None,0,1234])
async def test_journal_refresh_resolves_size_and_text_policy(tmp_path,size_override,text_override):
    # VaultService.ts:73-81; journal settings-realisation boundary must update
    # the same TextAccessPolicy setter used at startup, without losing overrides.
    import json
    from tests.test_storage_journal import make_backend, healthy_bucket
    from tests.test_selection_settings import milestone
    from storage_journal import MILESTONE_KEY
    b,s=make_backend(tmp_path,healthy_bucket())
    b.sync_max_size_mb_override=size_override
    if text_override is not None:b.text_scan_max_bytes=text_override
    s._objects[MILESTONE_KEY]=json.dumps(milestone(syncMaxSizeInMB=300.25)).encode()
    try:
        await b.startup_check()
        assert b.sync_max_size_mb==(300.25 if size_override is None else size_override)
        s._objects[MILESTONE_KEY]=json.dumps(milestone(syncMaxSizeInMB=350.5)).encode()
        await b.refresh(force=True)
        effective=350.5 if size_override is None else size_override
        assert b.sync_max_size_mb==effective
        assert b.text_scan_max_bytes==(text_override if text_override is not None else max(256*1024*1024,int(effective*1024*1024)))
    finally:await b.close()
