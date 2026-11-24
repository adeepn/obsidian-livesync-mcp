"""Read-only compatibility and fail-before-mutation writer gates."""
import copy
import json
from contextlib import asynccontextmanager

import pytest

from content_splitter import ContentSplitter
from storage_base import StorageError, UnsupportedVault
from tests.test_storage_couchdb import fake_vault
from tests.test_storage_journal import (
    make_backend, MILESTONE_KEY, SYNC_PARAMETERS_KEY, params_blob, journal_blob,
)


@asynccontextmanager
async def vault(tmp_path, kind, settings):
    milestone = {'tweak_values': {'PREFERRED': {'pathNormalizationVersion': 0, **settings}}}
    note = {'_id': 'old.md', '_rev': '1-a', 'path': 'old.md',
            'type': 'notes', 'data': 'readable', 'ctime': 1, 'mtime': 1, 'size': 8}
    if kind == 'couchdb':
        async with fake_vault({'old.md': note,
                '_local/obsydian_livesync_milestone': milestone}) as (backend, raw, log):
            async def snapshot():
                response = await raw.get('obsidian/_all_docs', params={'include_docs': 'true'})
                return response.json()
            yield backend, snapshot, lambda: [r for r in log if r.method in ('PUT', 'DELETE')]
    else:
        backend, storage = make_backend(tmp_path, {
            MILESTONE_KEY: json.dumps(milestone).encode(),
            SYNC_PARAMETERS_KEY: params_blob(),
            '1-docs.jsonl.gz': journal_blob([note]),
        })
        async def snapshot():
            return (copy.deepcopy(storage._objects),
                    list(backend._store._conn.iterdump()),
                    {p.name: p.read_bytes() for p in (tmp_path / 'state').rglob('*')
                     if p.is_file() and not p.name.startswith('state.sqlite')})
        try:
            yield backend, snapshot, lambda: storage.put_calls
        finally:
            await backend.close()


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('settings', [
    {'idDerivationVersion': 3}, {'hashAlg': 'unknown'},
    {'idDerivationVersion': 2, 'hashAlg': 'unknown'},
    {'idDerivationVersion': 2},
    {'idDerivationVersion': 1},
])
async def test_unsupported_writer_keeps_reads_and_blocks_all_mutations(tmp_path, kind, settings, monkeypatch):
    import server
    async with vault(tmp_path, kind, settings) as (backend, snapshot, writes):
        await backend.startup_check()
        await backend.refresh(force=True)
        assert [n['path'] for n in await backend.list_notes()] == ['old.md']
        assert (await backend.read_note('old.md'))['text'] == 'readable'
        assert [text async for _, text in backend.iterate_texts()] == ['readable']
        info = await backend.backend_info()
        assert info['writer_supported'] is False
        reason = info['writer_unsupported_reason']
        for key, value in settings.items():
            assert f'{key}={value}' in reason
        assert 'reads, search and replay work' in reason
        before = await snapshot()
        calls = [
            backend.write_note('new.md', 'new', create=True),
            backend.write_attachment('new.png', b'new', create=True),
            backend.delete_note('old.md'),
            backend.rename_note('old.md', 'new.md'),
            backend.copy_note('old.md', 'new.md'),
        ]
        for call in calls:
            with pytest.raises(StorageError, match='peer writes'):
                await call
        monkeypatch.setattr(server, 'backend', backend)
        result = await server.obsidian_create_note('new.md', 'new')
        assert result == 'Error: new.md: ' + reason
        assert 'Traceback' not in result
        assert await snapshot() == before
        assert not writes()


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_supported_writer_info_and_write(tmp_path, kind):
    async with vault(tmp_path, kind, {'chunkSplitterVersion': 'v3-rabin-karp',
                                     'hashAlg': 'xxhash64'}) as (backend, _, writes):
        await backend.startup_check()
        info = await backend.backend_info()
        assert info['writer_supported'] is True
        assert info['writer_unsupported_reason'] is None
        await backend.write_note('new.md', 'new', create=True)
        assert (await backend.read_note('new.md'))['text'] == 'new'
        assert writes()


@pytest.mark.parametrize('key,value', [('customChunkSize', 1e308),
    ('customChunkSize', 10**400), ('minimumChunkSize', 10**400)])
def test_overflow_is_keyed_value_error(key, value):
    with pytest.raises(ValueError, match=key):
        ContentSplitter({key: value})


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('key,value', [('customChunkSize', 1e308),
                                     ('minimumChunkSize', 10**400)])
async def test_backend_overflow_is_clean_storage_error(tmp_path, kind, key, value):
    async with vault(tmp_path, kind, {key: value}) as (backend, _, writes):
        with pytest.raises(UnsupportedVault, match=key):
            await backend.startup_check()
        assert not writes()


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('settings', [{'idDerivationVersion': 3}, {'hashAlg': 'unknown'}])
async def test_mcp_lifespan_serves_read_and_clean_writer_error(tmp_path, kind, settings, monkeypatch):
    import server
    from config import load_config
    async with vault(tmp_path, kind, settings) as (backend, snapshot, writes):
        monkeypatch.setattr(server, 'get_backend', lambda cfg: backend)
        app = server.create_app(load_config())
        async with app.settings.lifespan(app):
            await backend.refresh(force=True)
            before = await snapshot()
            result = await app.call_tool('obsidian_create_note', {'path': 'new.md', 'content': 'new'})
            # FastMCP returns (content blocks, structured content).
            blocks = result[0] if isinstance(result, tuple) else result
            message = blocks[0].text
            assert message.startswith('Error: new.md: vault uses ')
            for key, value in settings.items():
                assert f'{key}={value}' in message
            result = await app.call_tool('obsidian_fetch', {'path': 'old.md'})
            blocks = result[0] if isinstance(result, tuple) else result
            assert json.loads(blocks[0].text)['content'] == 'readable'
            assert await snapshot() == before
            assert not writes()


@pytest.mark.parametrize('tool,args', [
    ('obsidian_create_note', ('new.md', 'new')),
    ('obsidian_update_note', ('old.md', 'overwrite', 'new')),
    ('obsidian_delete_note', ('old.md',)),
    ('obsidian_move_note', ('old.md', 'new.md')),
    ('obsidian_duplicate_note', ('old.md', 'new.md')),
    ('obsidian_upload_attachment', ('new.png', 'bmV3')),
    ('obsidian_create_from_template', ('old.md', 'new.md')),
    ('obsidian_canvas_node', ('old.canvas',)),
    ('obsidian_canvas_edge', ('old.canvas',)),
    ('obsidian_manage_properties', ('old.md', 'set', '{}')),
    ('obsidian_manage_tags', ('old.md', 'add', 'tag')),
])
async def test_rejected_write_tool_does_not_replay_pending_journal(tmp_path, monkeypatch, tool, args):
    import server
    async with vault(tmp_path, 'journal', {'idDerivationVersion': 3}) as (backend, snapshot, writes):
        await backend.startup_check()
        monkeypatch.setattr(server, 'backend', backend)
        assert backend._store.winner('old.md') is None
        before = await snapshot()
        result = await getattr(server, tool)(*args)
        assert result.startswith('Error: ')
        assert 'vault uses idDerivationVersion=3; peer writes' in result
        for path in (args[0], args[1] if tool in ('obsidian_move_note', 'obsidian_create_from_template') else None):
            if path:
                assert path in result
        assert await snapshot() == before
        assert not writes()
        # Read actions on mixed tools still refresh and reconstruct content.
        assert await server.obsidian_manage_properties('old.md') == '{}'
        assert backend._store.winner('old.md') is not None
