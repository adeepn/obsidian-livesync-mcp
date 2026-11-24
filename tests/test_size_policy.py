"""Device selection size policy; publication and replay must stay asymmetric."""
import json
import pytest
import server
from tests.test_upload_attachment import backend
from tests.test_storage_journal import make_backend, healthy_bucket, MILESTONE_KEY
from storage_journal import JournalBackend


@pytest.mark.parametrize('method,path', [('write_note', 'n.md'), ('write_attachment', 'n.pdf')])
@pytest.mark.parametrize('maximum,size,skipped', [(1, 1048576, False), (1, 1048577, True),
    (.5, 524288, False), (.5, 524289, True), (0, 4096, False), (-1, 4096, False),
    (.000001, 2, True)])
async def test_device_size_boundaries(backend, method, path, maximum, size, skipped):
    from storage_base import WriteSkipped
    backend.sync_max_size_mb = maximum
    operation = getattr(backend, method)
    if skipped:
        with pytest.raises(WriteSkipped) as caught:
            await operation(path, b'x' * size, create=True)
        assert caught.value.size == size
        assert await backend.read_note(path) is None
    else:
        meta = await operation(path, b'x' * size, create=True)
        assert meta['size'] == size


@pytest.mark.parametrize('method', ['write_note', 'write_attachment'])
async def test_skip_keeps_revision_and_publishes_nothing(backend, method, monkeypatch):
    from storage_base import WriteSkipped
    meta = await backend.write_note('a.pdf', b'old', create=True)
    backend.sync_max_size_mb = 4 / 1048576
    def forbidden(*args, **kwargs):
        pytest.fail('oversized input reached publication')
    monkeypatch.setattr(backend, '_publish' if isinstance(backend, JournalBackend) else '_put_doc', forbidden)
    with pytest.raises(WriteSkipped):
        await getattr(backend, method)('a.pdf', b'larger', expected_rev=meta['rev'])
    assert (await backend.read_note('a.pdf'))['rev'] == meta['rev']
    assert (await backend.read_note('a.pdf'))['data'] == b'old'


@pytest.mark.parametrize('existing_target', [False, True])
async def test_oversized_rename_refuses_without_publication(backend, monkeypatch, existing_target):
    from storage_base import RenameRefused
    before = await backend.write_note('a.pdf', b'existing', create=True)
    if existing_target:
        await backend.write_note('moved.pdf', b'target', create=True)
    target = await backend.read_note('moved.pdf')
    backend.sync_max_size_mb = 1 / 1048576
    def forbidden(*args, **kwargs):
        pytest.fail('refused rename reached publication')
    monkeypatch.setattr(backend, '_publish' if isinstance(backend, JournalBackend) else '_put_doc', forbidden)
    with pytest.raises(RenameRefused, match='move was not performed'):
        await backend.rename_note('a.pdf', 'moved.pdf', overwrite=True)
    source = await backend.read_note('a.pdf')
    assert source['rev'] == before['rev']
    assert source['data'] == b'existing'
    assert await backend.read_note('moved.pdf') == target


async def test_mcp_skip_is_structured_and_not_success(backend, monkeypatch):
    backend.sync_max_size_mb = 1 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_upload_attachment('scan.pdf', 'YWJj'))
    assert response['status'] == 'skipped'
    assert response['size'] == 3
    assert 'syncMaxSizeInMB' in response['message']
    assert await backend.read_note('scan.pdf') is None


async def test_large_mult_piece_replay_under_different_receiver_limit(tmp_path):
    objects = healthy_bucket()
    milestone = json.loads(objects[MILESTONE_KEY])
    milestone['tweak_values']['PREFERRED'].update(syncMaxSizeInMB=50, chunkSplitterVersion='v3-rabin-karp')
    objects[MILESTONE_KEY] = json.dumps(milestone).encode()
    writer, storage = make_backend(tmp_path / 'writer', objects)
    reader = JournalBackend(storage, tmp_path / 'reader')
    data = bytes(range(256)) * (40 * 1024 * 1024 // 256)
    try:
        await writer.startup_check()
        assert writer.sync_max_size_mb == 50
        await writer.write_attachment('large.pdf', data, create=True)
        assert len(writer._store.winner('large.pdf')['children']) > 1
        await reader.startup_check()
        reader.sync_max_size_mb = .5
        await reader.refresh(force=True)
        assert (await reader.read_note('large.pdf'))['data'] == data
    finally:
        await writer.close()
        await reader.close()


async def test_40_mib_complete_on_both_backends(backend):
    backend.sync_max_size_mb = 50
    data = b'\x00\xff\x80\x7f' * (10 * 1024 * 1024)
    meta = await backend.write_attachment('big.pdf', data, create=True)
    assert meta['size'] == 41943040
    assert (await backend.read_note('big.pdf'))['data'] == data


@pytest.mark.parametrize('settings,want', [
    ({'tweak_values': {'PREFERRED': {'syncMaxSizeInMB': .5}, 'device': {'syncMaxSizeInMB': 100}}}, .5),
    ({'tweak_values': {'device': {'syncMaxSizeInMB': 100}}}, 100),
    ({'syncMaxSizeInMB': 0}, 0),
    ({'pathNormalizationVersion': 0}, 50),
])
def test_size_settings_resolution(settings, want):
    from livesync_format import resolve_sync_max_size
    assert resolve_sync_max_size(settings) == want


def test_conflicting_device_limits_refuse_to_guess():
    from livesync_format import resolve_sync_max_size, UnsupportedVaultSettings
    with pytest.raises(UnsupportedVaultSettings, match='conflicting syncMaxSizeInMB'):
        resolve_sync_max_size({'tweak_values': {'a': {'syncMaxSizeInMB': 50}, 'b': {'syncMaxSizeInMB': 100}}})


async def test_explicit_size_override_survives_startup(backend):
    backend.sync_max_size_mb_override = 0
    await backend.startup_check()
    assert backend.sync_max_size_mb == 0
    assert backend.sync_max_size_mb_override == 0


async def test_oversized_malformed_upload_skips_before_decoding(backend, monkeypatch):
    import base64
    backend.sync_max_size_mb = 1 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_upload_attachment('malformed.md', base64.b64encode(b'\xff\xff').decode()))
    assert response['status'] == 'skipped'
    assert response['size'] == 2
    assert await backend.read_note('malformed.md') is None


@pytest.mark.parametrize('limit,skipped', [(1, True), (2, True), (3, False)])
async def test_lossy_ingress_checks_raw_and_converted_blob_size(backend, monkeypatch, limit, skipped):
    await backend.write_note('malformed.md', 'x', create=True)
    before = await backend.read_note('malformed.md')
    backend.sync_max_size_mb = limit / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    if skipped:
        def forbidden(*args, **kwargs):
            pytest.fail('oversized converted Blob reached publication')
        monkeypatch.setattr(backend, '_publish' if isinstance(backend, JournalBackend) else '_put_doc', forbidden)
    response = json.loads(await server.obsidian_upload_attachment('malformed.md', '/w==', lossy_text=True, overwrite=True))
    assert response['size'] == 3
    if skipped:
        assert response['status'] == 'skipped'
        assert 'new content was NOT saved' in response['message']
        assert await backend.read_note('malformed.md') == before
    else:
        assert response['replaced_bytes'] == 1
        assert (await backend.read_note('malformed.md'))['text'] == '\ufffd'


async def test_mcp_oversized_rename_reports_refusal(backend, monkeypatch):
    await backend.write_note('rename-source.pdf', b'existing', create=True)
    backend.sync_max_size_mb = 1 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    response = await server.obsidian_move_note('rename-source.pdf', 'rename-target.pdf')
    assert 'Error:' in response
    for detail in ['8 bytes', 'syncMaxSizeInMB', 'move was not performed', 'only copy']:
        assert detail in response
    assert (await backend.read_note('rename-source.pdf'))['data'] == b'existing'
    assert await backend.read_note('rename-target.pdf') is None


async def test_oversized_rename_unselected_source_is_preserved(backend):
    from storage_base import RenameRefused
    meta = await backend.write_note('excluded-source.pdf', b'existing', create=True)
    backend.selection_ignore = 'excluded-source'
    backend.sync_max_size_mb = 1 / 1048576
    with pytest.raises(RenameRefused):
        await backend.rename_note('excluded-source.pdf', 'outside-target.pdf')
    assert (await backend.read_note('excluded-source.pdf'))['rev'] == meta['rev']
    assert await backend.read_note('outside-target.pdf') is None


def test_raising_positive_limit_does_not_disable_selection():
    from storage_base import check_file_size, WriteSkipped
    with pytest.raises(WriteSkipped):
        check_file_size('large.pdf', 75 * 1048576, 50)
    check_file_size('large.pdf', 75 * 1048576, 100)
    check_file_size('large.pdf', 100 * 1048576, 100)
    with pytest.raises(WriteSkipped):
        check_file_size('large.pdf', 100 * 1048576 + 1, 100)


async def test_note_create_and_update_tools_report_skip(backend, monkeypatch):
    meta = await backend.write_note('tool-existing.md', 'old', create=True)
    backend.sync_max_size_mb = 4 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    for result in [await server.obsidian_create_note('tool-new.md', 'larger'),
                   await server.obsidian_update_note('tool-existing.md', 'overwrite', 'larger')]:
        assert json.loads(result)['status'] == 'skipped'
    assert await backend.read_note('tool-new.md') is None
    assert (await backend.read_note('tool-existing.md'))['rev'] == meta['rev']


@pytest.mark.parametrize('operation', [
    'create', 'upload', 'copy', 'move', 'filtered_move', 'missing_delete',
    'overwrite', 'append', 'prepend', 'after-heading', 'search-replace',
    'properties_set', 'properties_delete', 'tags_add', 'tags_remove',
    'node_add', 'node_remove', 'edge_add', 'edge_remove', 'template',
])
async def test_unsuccessful_tool_preserves_every_readable_note(backend, monkeypatch, operation):
    await backend.write_note('invariant.md', '---\nkey: value\ntags: [tag]\n---\n# heading\nold', create=True)
    await backend.write_note('target.md', 'target', create=True)
    await backend.write_note('invariant.canvas', json.dumps({
        'nodes': [{'id': 'a', 'type': 'text', 'text': 'old'}, {'id': 'b', 'type': 'text', 'text': 'other'}],
        'edges': [{'id': 'edge', 'fromNode': 'a', 'toNode': 'b'}],
    }), create=True)
    before = {note['path']: await backend.read_note(note['path']) for note in await backend.list_notes()}
    backend.sync_max_size_mb = 1 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    if operation == 'filtered_move':
        backend.selection_ignore = 'target'
    calls = {
        'create': lambda: server.obsidian_create_note('invariant.md', 'too large'),
        'upload': lambda: server.obsidian_upload_attachment('invariant.md', 'bGFyZ2U='),
        'copy': lambda: server.obsidian_duplicate_note('invariant.md', 'target.md'),
        'move': lambda: server.obsidian_move_note('invariant.md', 'target.md'),
        'filtered_move': lambda: server.obsidian_move_note('invariant.md', 'target.md'),
        'missing_delete': lambda: server.obsidian_delete_note('absent.md'),
        'properties_set': lambda: server.obsidian_manage_properties('invariant.md', 'set', '{"key":"larger"}'),
        'properties_delete': lambda: server.obsidian_manage_properties('invariant.md', 'delete', keys='key'),
        'tags_add': lambda: server.obsidian_manage_tags('invariant.md', 'add', 'new'),
        'tags_remove': lambda: server.obsidian_manage_tags('invariant.md', 'remove', 'tag'),
        'node_add': lambda: server.obsidian_canvas_node('invariant.canvas', text='new'),
        'node_remove': lambda: server.obsidian_canvas_node('invariant.canvas', 'remove', node_id='a'),
        'edge_add': lambda: server.obsidian_canvas_edge('invariant.canvas', from_node='a', to_node='b'),
        'edge_remove': lambda: server.obsidian_canvas_edge('invariant.canvas', 'remove', edge_id='edge'),
        'template': lambda: server.obsidian_create_from_template('invariant.md', 'new.md'),
    }
    if operation in ['overwrite', 'append', 'prepend', 'after-heading', 'search-replace']:
        result = await server.obsidian_update_note('invariant.md', operation, 'too large',
            **({'heading':'heading'} if operation == 'after-heading' else
               {'search':'old','replace':'larger'} if operation == 'search-replace' else {}))
    else:
        result = await calls[operation]()
    assert 'Error:' in result or 'not found' in result or json.loads(result)['status'] in ['skipped', 'refused']
    for path, note in before.items():
        after = await backend.read_note(path)
        assert after['data'] == note['data'], path
        assert after['rev'] == note['rev'], path


async def test_refused_oversized_move_remains_readable_on_fresh_journal_peer(tmp_path, monkeypatch):
    writer, storage = make_backend(tmp_path / 'writer', healthy_bucket())
    reader = JournalBackend(storage, tmp_path / 'reader')
    try:
        await writer.startup_check()
        original = b'\x00\xffonly synced copy\x80'
        meta = await writer.write_attachment('only-copy.pdf', original, create=True)
        writer.sync_max_size_mb = 1 / 1048576
        monkeypatch.setattr(server, 'backend', writer)
        result = await server.obsidian_move_note('only-copy.pdf', 'moved-copy.pdf')
        assert 'Error:' in result and 'move was not performed' in result
        await reader.startup_check()
        await reader.refresh(force=True)
        note = await reader.read_note('only-copy.pdf')
        assert note['rev'] == meta['rev']
        assert note['data'] == original
        assert await reader.read_note('moved-copy.pdf') is None
        assert 'only-copy.pdf' in [n['path'] for n in await reader.list_notes()]
    finally:
        await writer.close()
        await reader.close()


async def test_explicit_delete_of_oversized_note_remains_allowed(backend):
    await backend.write_attachment('delete-oversized.pdf', b'existing', create=True)
    backend.sync_max_size_mb = 1 / 1048576
    assert await backend.delete_note('delete-oversized.pdf')
    assert await backend.read_note('delete-oversized.pdf') is None
