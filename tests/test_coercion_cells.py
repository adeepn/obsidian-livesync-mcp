"""ContentSplitterBase.ts:68-71; chunks.ts:218-493,527-545 executed upstream cells."""
import base64
import json
from pathlib import Path
import pytest
from content_splitter import ContentSplitter
from tests.test_settings_matrix import matrix_vault

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/coercion_upstream.json').read_text())
ROWS = FIXTURE['rows']

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
        assert (await backend.read_note(row['payload']['path']))['text' if row['payload']['text'] else 'data'] == data
