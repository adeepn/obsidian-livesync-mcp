"""SettingService legacy migration must precede writer dispatch/comparison."""
import pytest
from content_splitter import ContentSplitter
from livesync_format import resolve_content_splitter, UnsupportedVaultSettings


@pytest.mark.parametrize('enable', [False, True])
@pytest.mark.parametrize('segmenter', [False, True])
def test_empty_version_migrates_legacy_flags_before_dispatch(enable, segmenter):
    settings = {'chunkSplitterVersion': '', 'enableChunkSplitterV2': enable, 'useSegmenter': segmenter}
    splitter = ContentSplitter(settings)
    expected = 'v2-segmenter' if enable and segmenter else 'v2' if enable else ''
    assert splitter.normalized_settings['chunkSplitterVersion'] == expected
    assert splitter.split('\n#', path='x.md') == (['\n#'] if enable else ['\n', '#'])
    assert settings['chunkSplitterVersion'] == ''


@pytest.mark.parametrize('version', ['v1', 'v2', 'v2-segmenter', 'v3-rabin-karp'])
@pytest.mark.parametrize('enable', [False, True])
@pytest.mark.parametrize('segmenter', [False, True])
def test_explicit_version_ignores_legacy_flags(version, enable, segmenter):
    splitter = ContentSplitter({'chunkSplitterVersion': version, 'enableChunkSplitterV2': enable, 'useSegmenter': segmenter})
    assert splitter.normalized_settings['chunkSplitterVersion'] == version


def test_resolver_compares_migrated_device_settings():
    splitter = resolve_content_splitter({'tweak_values': {
        'old': {'chunkSplitterVersion': '', 'enableChunkSplitterV2': True, 'useSegmenter': True},
        'new': {'chunkSplitterVersion': 'v2-segmenter'}}})
    assert splitter.normalized_settings['chunkSplitterVersion'] == 'v2-segmenter'
    assert splitter.split('\n#', path='x.md') == ['\n#']


def test_resolver_detects_conflict_after_migration():
    with pytest.raises(UnsupportedVaultSettings, match='conflicting chunk splitter'):
        resolve_content_splitter({'tweak_values': {
            'old': {'chunkSplitterVersion': '', 'enableChunkSplitterV2': True},
            'new': {'chunkSplitterVersion': 'v1'}}})


def test_unknown_version_is_normalized_once_without_legacy_remigration():
    splitter = resolve_content_splitter({'tweak_values': {'PREFERRED': {
        'chunkSplitterVersion': 'unknown', 'enableChunkSplitterV2': True, 'useSegmenter': True}}})
    assert splitter.normalized_settings['chunkSplitterVersion'] == ''
    assert splitter.split('\n#', path='x.md') == ['\n', '#']


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('segmenter', [False, True])
async def test_backend_resolves_migrated_writer(tmp_path, kind, segmenter):
    from tests.test_writer_support import vault
    async with vault(tmp_path, kind, {'chunkSplitterVersion': '', 'enableChunkSplitterV2': True, 'useSegmenter': segmenter}) as (backend, _, _):
        await backend.startup_check()
        assert backend._splitter.version == 'v2'
        await backend.write_note('migration.md', '\n#', create=True)
        doc = backend._store.winner('migration.md') if kind == 'journal' else await backend._get_doc('migration.md')
        assert doc['children'] == [backend._splitter.chunk_id_for_piece('\n#')]
        assert (await backend.read_note('migration.md'))['text'] == '\n#'


def test_normalizer_matches_pinned_setting_service():
    import json
    from pathlib import Path
    from content_splitter import normalize_splitter_settings
    fixture = json.loads((Path(__file__).parent / 'fixtures/splitter_migration_upstream.json').read_text())
    for vector in fixture['vectors']:
        assert normalize_splitter_settings(vector['input']) == vector['expected']


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('enable', [False, True])
@pytest.mark.parametrize('segmenter', [False, True])
async def test_unknown_vault_version_normalizes_without_second_migration(tmp_path, kind, enable, segmenter):
    from tests.test_writer_support import vault
    async with vault(tmp_path, kind, {'chunkSplitterVersion': 'unknown', 'enableChunkSplitterV2': enable, 'useSegmenter': segmenter}) as (backend, _, _):
        await backend.startup_check()
        assert (await backend.backend_info())['writer_supported'] is True
        assert backend._splitter.normalized_settings['chunkSplitterVersion'] == ''
        await backend.write_note('unknown.md', '\n#', create=True)
        doc = backend._store.winner('unknown.md') if kind == 'journal' else await backend._get_doc('unknown.md')
        assert doc['children'] == [backend._splitter.chunk_id_for_piece(p) for p in ['\n', '#']]
