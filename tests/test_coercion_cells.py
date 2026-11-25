"""ContentSplitterBase.ts:68-71; chunks.ts:218-493,527-545 executed upstream cells."""
import base64
import json
from pathlib import Path
import pytest
from content_splitter import ContentSplitter
from tests.test_settings_matrix import matrix_vault

FIXTURES = Path(__file__).parent / 'fixtures'
CELL_FIXTURES = (
    'coercion_upstream.json', 'coercion_edges_upstream.json',
    'coercion_cross_upstream.json', 'coercion_ingress_upstream.json',
    'coercion_overflow_upstream.json', 'coercion_zero_upstream.json',
)
ROWS = [row for name in CELL_FIXTURES
        for row in json.loads((FIXTURES / name).read_text())['rows']]

for row in ROWS:
    if 'settings_json' in row:
        row['settings']=json.loads(row['settings_json'])

def refuses(row):
    # chunks.ts:418-432: NaN binary cap terminates with an empty leaf, losing input.
    return row['status'] != 'defined' or (row['mode'] == 'binary' and b''.join(base64.b64decode(p) for p in row['pieces']) != base64.b64decode(row['payload']['base64']))

@pytest.mark.parametrize('row', ROWS)
def test_coercion_partition(row):
    raw = base64.b64decode(row['payload']['base64'])
    splitter = ContentSplitter(row['settings'])
    if refuses(row):
        with pytest.raises(ValueError, match='customChunkSize|minimumChunkSize'):
            splitter.split(raw, path=row['payload']['path'], text_blob=row['payload']['text'])
        return
    pieces = splitter.split(raw, path=row['payload']['path'], text_blob=row['payload']['text'])
    assert pieces == row['split_pieces']
    assert [splitter.chunk_id_for_piece(p) for p in pieces] == row['split_children']

@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
@pytest.mark.parametrize('row', ROWS)
async def test_coercion_settings_matrix(tmp_path, kind, row):
    async with matrix_vault(tmp_path, kind, {'pathNormalizationVersion':0, 'handleFilenameCaseSensitive':False, **row['settings']}) as (backend, doc, chunks, log, storage):
        # Arrays/objects compare by reference in tweak negotiation; exercise
        # the documented mismatch override to reach the upstream writer setting.
        backend.disable_checking_config_mismatch = True
        await backend.startup_check()
        before = await chunks()
        raw = base64.b64decode(row['payload']['base64'])
        data = raw.decode() if row['payload']['text'] else raw
        if refuses(row):
            with pytest.raises(ValueError, match='customChunkSize|minimumChunkSize'):
                await backend._write_content(row['payload']['path'], data, len(raw), 'plain' if row['payload']['text'] else 'newnote', create=True, overwrite=False)
            assert await chunks() == before
            return
        result = await backend._write_content(row['payload']['path'], data, len(raw), 'plain' if row['payload']['text'] else 'newnote', create=True, overwrite=False)
        stored = await doc(row['payload']['path'])
        assert stored['children'] == row['children']
        assert (await backend.read_note(row['payload']['path']))['text' if row['payload']['text'] else 'data'] == (''.join(row['pieces']) if row['payload']['text'] else data)

@pytest.mark.parametrize('minimum', [[1.0], [2.0]])
def test_array_numeric_representation_uses_js_primitive(minimum):
    # JSON has one Number type: execute the same upstream [1]/[2] cell.
    row = next(r for r in ROWS if r['key'] == 'minimumChunkSize' and r['value'] == minimum
               and r['version'] == 'v2' and r['mode'] == 'plain')
    settings = {**row['settings'], 'minimumChunkSize': minimum}
    raw = base64.b64decode(row['payload']['base64'])
    assert ContentSplitter(settings).split(raw, path=row['payload']['path'], text_blob=True) == row['split_pieces']


@pytest.mark.parametrize('row',json.loads(Path('tests/fixtures/chunk_parameters_upstream.json').read_text())['rows'])
def test_exact_js_custom_cap_parameter(row):
    # ContentSplitterBase.ts:68 is executed directly, not inferred from short partitions.
    import math
    splitter=ContentSplitter({'customChunkSize':json.loads(row['input_json']),'chunkSplitterVersion':'v3-rabin-karp'})
    if 'error_class' in row:
        with pytest.raises(ValueError,match='customChunkSize'):
            splitter.split('A')
    else:
        expected=row['piece_size']
        actual=splitter.absolute_max_piece_size
        assert ('NaN' if math.isnan(actual) else 'Infinity' if actual==math.inf else '-Infinity' if actual==-math.inf else actual)==expected


@pytest.mark.parametrize('row',json.loads(Path('tests/fixtures/chunk_parameters_upstream.json').read_text())['rows'])
def test_native_primitive_number_and_addition(row):
    from content_splitter import _splitter_property_key, _minimum_number, _js_add
    from wire_encoding import js_number_string
    value=json.loads(row['input_json'])
    if 'error_class' in row:
        with pytest.raises(ValueError):_splitter_property_key(value)
    else:
        assert _splitter_property_key(value)==row['primitive']
        assert js_number_string(_minimum_number(value))==row['number']
        addition=_js_add(value,value)
        assert (js_number_string(addition) if row['addition_type']=='number' else addition)==row['addition']


def test_coercion_fixture_generator_provenance():
    import hashlib
    expected = hashlib.sha256(Path('scripts/extract_coercion_cells.mjs').read_bytes()).hexdigest()
    for name in CELL_FIXTURES:
        fixture = json.loads((FIXTURES / name).read_text())
        assert fixture['generator_sha256'] == expected, 'Regenerate the coercion fixtures with scripts/extract_coercion_cells.mjs and its recorded mode'
        assert fixture['provenance']['upstream_commit'] == '484c164450d61deb2e301582b60cb9b5f9fa977f'
