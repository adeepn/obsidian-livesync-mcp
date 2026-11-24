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


async def test_existing_oversized_rename_retires_source_but_delete_is_allowed(backend):
    from storage_base import WriteSkipped, RenameOutOfSync
    await backend.write_note('a.pdf', b'existing', create=True)
    backend.sync_max_size_mb = 1 / 1048576
    with pytest.raises(WriteSkipped):
        await backend.copy_note('a.pdf', 'copy.pdf')
    with pytest.raises(RenameOutOfSync) as caught:
        await backend.rename_note('a.pdf', 'moved.pdf')
    assert caught.value.source_retired
    assert await backend.read_note('a.pdf') is None
    assert await backend.read_note('moved.pdf') is None
    backend.sync_max_size_mb = 0
    await backend.write_note('delete.pdf', b'existing', create=True)
    backend.sync_max_size_mb = 1 / 1048576
    assert await backend.delete_note('delete.pdf')


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


async def test_lossy_ingress_uses_raw_size_before_conversion(backend, monkeypatch):
    backend.sync_max_size_mb = 1 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_upload_attachment('malformed.md', '/w==', lossy_text=True))
    assert response['size'] == 3
    assert response['replaced_bytes'] == 1
    assert (await backend.read_note('malformed.md'))['text'] == '\ufffd'


async def test_mcp_oversized_rename_reports_source_retirement(backend, monkeypatch):
    await backend.write_note('rename-source.pdf', b'existing', create=True)
    backend.sync_max_size_mb = 1 / 1048576
    monkeypatch.setattr(server, 'backend', backend)
    response = json.loads(await server.obsidian_move_note('rename-source.pdf', 'rename-target.pdf'))
    assert response['status'] == 'moved_out_of_sync'
    assert response['source_retired'] is True
    assert response['destination_published'] is False
    assert await backend.read_note('rename-source.pdf') is None
    assert await backend.read_note('rename-target.pdf') is None


async def test_oversized_rename_unselected_source_is_preserved(backend):
    from storage_base import RenameOutOfSync
    meta = await backend.write_note('excluded-source.pdf', b'existing', create=True)
    backend.selection_ignore = 'excluded-source'
    backend.sync_max_size_mb = 1 / 1048576
    with pytest.raises(RenameOutOfSync) as caught:
        await backend.rename_note('excluded-source.pdf', 'outside-target.pdf')
    assert caught.value.source_retired is False
    assert (await backend.read_note('excluded-source.pdf'))['rev'] == meta['rev']
    assert await backend.read_note('outside-target.pdf') is None


async def test_oversized_rename_retirement_failure_preserves_source(backend, monkeypatch):
    from storage_base import StorageError
    meta = await backend.write_note('failure-source.pdf', b'existing', create=True)
    backend.sync_max_size_mb = 1 / 1048576
    def sync_failure(*args, **kwargs):
        raise StorageError('injected retirement failure')
    async def async_failure(*args, **kwargs):
        raise StorageError('injected retirement failure')
    monkeypatch.setattr(backend, '_publish' if isinstance(backend, JournalBackend) else '_soft_delete',
                        sync_failure if isinstance(backend, JournalBackend) else async_failure)
    with pytest.raises(StorageError, match='injected retirement failure'):
        await backend.rename_note('failure-source.pdf', 'failure-target.pdf')
    assert (await backend.read_note('failure-source.pdf'))['rev'] == meta['rev']
    assert await backend.read_note('failure-target.pdf') is None


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
