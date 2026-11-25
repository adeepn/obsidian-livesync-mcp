"""G3: pinned chunks.ts:218-254,297-432,436-545, including empty inputs.

Cell evidence: scripts/extract_legacy_cells.mjs + legacy_cells_upstream.json.
Expected partitions and IDs are executed upstream values, never Python output.
"""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from content_splitter import ContentSplitter
from tests.settings_oracle_inputs import G3_CASES, case_id, settings_for, grid_payload, grid_path


def runtime_settings(case):
    settings = settings_for(case)
    if settings.get('minimumChunkSize') == '__G3_NaN__':
        settings['minimumChunkSize'] = float('nan')
    if settings.get('minimumChunkSize') == '__G3_INFINITY__':
        settings['minimumChunkSize'] = 10**400
    return settings


@pytest.mark.parametrize('case', G3_CASES, ids=case_id)
def test_terminating_legacy_cell(case):
    from tests.settings_oracle import GRID, assert_partition
    vector = GRID[case_id(case)]
    splitter = ContentSplitter(runtime_settings(case))
    pieces = splitter.split(grid_payload(case[-1])[1], path=grid_path(case[-1]))
    assert_partition(pieces, [splitter.chunk_id_for_piece(p) for p in pieces], {
        'pieces': vector['split_pieces'], 'children': vector['split_children']})


@pytest.mark.parametrize('version,custom,minimum,data,path,setting', [
    ('v1', -2, 20, 'abc', 'a.md', 'customChunkSize'),
    ('v1', -1, 20, 'abc', 'a.svg', 'customChunkSize'),
    ('v1', -1, 20, b'abc', 'a.png', 'customChunkSize'),
    ('v2', -1, 20, 'abc', 'a.svg', 'customChunkSize'),
    ('v2', 0, 0, 'abc', 'a.md', 'minimumChunkSize'),
    ('v2', 0, '', 'abc', 'a.md', 'minimumChunkSize'),
    ('v2', 0, None, 'abc', 'a.md', 'minimumChunkSize'),
])
def test_looping_cell_names_plugin_and_recovery(version, custom, minimum, data, path, setting):
    splitter = ContentSplitter({'chunkSplitterVersion':version, 'customChunkSize':custom,
                                'minimumChunkSize':minimum})
    with pytest.raises(ValueError) as caught:
        splitter.split(data, path=path)
    assert setting in str(caught.value)
    assert 'plugin itself does not terminate' in str(caught.value)
    assert 'use' in str(caught.value)
    assert caught.value.setting_keys == (setting,)


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
@pytest.mark.parametrize('case', G3_CASES, ids=case_id)
async def test_cell_backend_lifecycle(tmp_path, kind, case):
    # EntryManagerImpls.ts:280 discards empty leaves; chunks.ts:234-240 can
    # split pairs. journalRecords.ts:70-76 replaces lone units on leaf wire.
    from tests.settings_oracle import GRID, expected_pieces
    from tests.test_settings_matrix import matrix_vault, pieces_for
    vector = GRID[case_id(case)]
    settings = runtime_settings(case)
    data = grid_payload(case[-1])[1]
    path = grid_path(case[-1])
    expected = expected_pieces(vector)
    if kind == 'journal':
        # JS TextEncoder USVString conversion, independently executed in the
        # committed surrogate_wire_upstream fixture; keep original IDs.
        if case[-1] == 'g3-surrogate':
            wire = json.loads((Path(__file__).parent / 'fixtures/legacy_wire_cells_upstream.json').read_text())
            projected = next(r for r in wire['rows'] if r['id'] == case_id(case))
            assert projected['children'] == vector['children']
            expected = projected['pieces']
    async with matrix_vault(tmp_path, kind, settings) as (backend, doc, _, _, _):
        if case[4] == '__G3_NaN__':
            # tweak.compatibility.ts:47-65 uses JS equality: NaN != NaN.
            # Exercise the explicit ordinary-mismatch bypass, as on devices.
            backend.disable_checking_config_mismatch = True
        await backend.startup_check()
        async def publish():
            if isinstance(data, str) and not path.endswith('.md'):
                await backend._write_content(path, data, len(data.encode()), 'plain',
                                             create=True, overwrite=False)
            else:
                await backend.write_note(path, data, create=True)
        if kind == 'couchdb' and any(0xD800 <= ord(c) <= 0xDFFF for p in expected for c in p):
            from storage_base import WriterParameterRefused
            with pytest.raises(WriterParameterRefused, match='unpaired UTF-16 surrogate'):
                await publish()
            assert await backend._get_doc(backend._path2id(path)) is None
            return
        await publish()
        metadata = await doc(path)
        assert metadata['children'] == vector['children']
        stored = await pieces_for(backend, metadata['children'])
        assert stored == expected
        field = 'data' if isinstance(data, bytes) else 'text'
        before = await backend.read_note(path)
        reconstructed = (b''.join(base64.b64decode(p) for p in expected)
                         if isinstance(data, bytes) else ''.join(expected).encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'surrogatepass'))
        assert before[field] == reconstructed
        moved = 'moved-' + path
        await backend.rename_note(path, moved)
        assert await backend.read_note(path) is None
        assert (await backend.read_note(moved))[field] == reconstructed
        assert (await doc(moved))['children'] == vector['children']
        assert await backend.delete_note(moved) is True
        assert await backend.read_note(moved) is None


CELL_FILE = Path(__file__).parent / 'fixtures/legacy_cells_upstream.json'
# This committed fixture is generated in isolated upstream child processes.
CELL_EVIDENCE = json.loads(CELL_FILE.read_text())
SUPPLEMENTAL_EVIDENCE = json.loads((CELL_FILE.parent / 'legacy_bom_cells_upstream.json').read_text())
ALL_CELL_ROWS = CELL_EVIDENCE['rows'] + SUPPLEMENTAL_EVIDENCE['rows']
LOOP_CELLS = [r for r in ALL_CELL_ROWS if r['status'] in ('time_limit', 'step_limit')]


def test_complete_cell_evidence():
    rows = CELL_EVIDENCE['rows']
    assert CELL_EVIDENCE['generator_sha256'] == hashlib.sha256(Path('scripts/extract_legacy_cells.mjs').read_bytes()).hexdigest()
    assert len(rows) == 864
    assert len({r['id'] for r in rows}) == 864
    assert CELL_EVIDENCE['provenance']['upstream_commit'] == '484c164450d61deb2e301582b60cb9b5f9fa977f'
    assert CELL_EVIDENCE['provenance']['decoder_dependency']['integrity'].startswith('sha512-')
    assert CELL_EVIDENCE['provenance']['wasm_dependency']['version'] == '1.1.0'
    assert CELL_EVIDENCE['provenance']['wasm_dependency']['integrity'].startswith('sha512-')
    wire = json.loads((CELL_FILE.parent / 'legacy_wire_cells_upstream.json').read_text())
    assert wire['inputs_sha256'] == hashlib.sha256((CELL_FILE.parent / 'settings_oracle_upstream.json.gz').read_bytes()).hexdigest()
    assert len(wire['rows']) == 3
    assert len(SUPPLEMENTAL_EVIDENCE['rows']) == 96
    assert len({r['id'] for r in SUPPLEMENTAL_EVIDENCE['rows']}) == 96
    assert SUPPLEMENTAL_EVIDENCE['provenance'] == CELL_EVIDENCE['provenance']
    assert {r['status'] for r in rows} == {'defined', 'step_limit', 'time_limit'}
    for row in LOOP_CELLS:
        assert row['proof'].startswith('chunks.ts:')
        assert not row['empty']
        assert row['version'] != 'v3-rabin-karp'
    # NaN is a tagged recipe value in JSON, materialized by the generator.
    assert len([r for r in rows if r['settings']['minimumChunkSize'] == '__G3_NaN__']) == 144


def cell_run(module, row):
    settings = dict(row['settings'])
    if settings['minimumChunkSize'] == '__G3_NaN__':
        settings['minimumChunkSize'] = float('nan')
    data = base64.b64decode(row['payload']['base64'])
    if row['payload']['text']:
        data = data.decode()
    splitter = module.ContentSplitter(settings)
    pieces = splitter.split(data, path=row['payload']['path'])
    return pieces, [splitter.chunk_id_for_piece(p) for p in pieces]


@pytest.mark.parametrize('row', ALL_CELL_ROWS, ids=lambda r: r['id'])
def test_every_upstream_cell(row):
    import content_splitter
    if row['status'] == 'defined':
        assert cell_run(content_splitter, row) == (row['split_pieces'], row['split_children'])
    else:
        with pytest.raises(ValueError, match='plugin itself does not terminate'):
            cell_run(content_splitter, row)


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
@pytest.mark.parametrize('row', LOOP_CELLS, ids=lambda r: r['id'])
async def test_loop_cell_preserves_backend(tmp_path, kind, row):
    from tests.test_settings_matrix import matrix_vault
    settings = {'pathNormalizationVersion':0, 'handleFilenameCaseSensitive':False, **row['settings']}
    if settings['minimumChunkSize'] == '__G3_NaN__':
        settings['minimumChunkSize'] = float('nan')
    path = row['payload']['path']
    data = base64.b64decode(row['payload']['base64'])
    if row['payload']['text']:
        data = data.decode()
    async with matrix_vault(tmp_path, kind, settings) as (backend, doc, chunks, log, storage):
        backend.disable_checking_config_mismatch = True
        await backend.startup_check()
        async def write(value, create=False):
            if row['input'] == 'text':
                return await backend._write_content(path, value, len(value.encode()), 'plain',
                                                    create=create, overwrite=False)
            return await backend.write_note(path, value, create=create)
        await write(b'' if isinstance(data, bytes) else '', create=True)
        before = await backend.read_note(path)
        metadata = await doc(path)
        stored = await chunks()
        def mutations():
            return len(log) if storage else sum(r.method in ('PUT','DELETE') or
                (r.method == 'POST' and not r.url.path.endswith('/_all_docs')) for r in log)
        count = mutations()
        with pytest.raises(ValueError, match='plugin itself does not terminate') as caught:
            await write(data)
        assert caught.value.setting_keys in (('customChunkSize',), ('minimumChunkSize',))
        assert mutations() == count
        assert await doc(path) == metadata
        assert await chunks() == stored
        assert await backend.read_note(path) == before


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
@pytest.mark.parametrize('row', [r for r in SUPPLEMENTAL_EVIDENCE['rows'] if r['status'] == 'defined'],
                         ids=lambda r: r['id'])
async def test_supplemental_backend_lifecycle(tmp_path, kind, row):
    # chunks.ts:234-250,288-313,350-371: decoded-empty and single-unit
    # grouped inputs retain distinct Blob size and persisted-child semantics.
    from tests.test_settings_matrix import matrix_vault, pieces_for
    settings = {'pathNormalizationVersion':0, 'handleFilenameCaseSensitive':False, **row['settings']}
    data = base64.b64decode(row['payload']['base64']).decode()
    path = row['payload']['path']
    async with matrix_vault(tmp_path, kind, settings) as (backend, doc, _, _, _):
        await backend.startup_check()
        await backend.write_note(path, data, create=True)
        metadata = await doc(path)
        assert metadata['size'] == len(data.encode())
        assert metadata['children'] == row['children']
        assert await pieces_for(backend, metadata['children']) == row['pieces']
        expected = ''.join(row['pieces'])
        assert (await backend.read_note(path))['text'] == expected
        moved = 'moved-' + path
        await backend.rename_note(path, moved)
        assert await backend.read_note(path) is None
        assert (await doc(moved))['children'] == row['children']
        assert (await backend.read_note(moved))['text'] == expected
        assert await backend.delete_note(moved) is True
        assert await backend.read_note(moved) is None
