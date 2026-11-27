"""F13: chunks.ts:234-250,297-313,350-379,555-556,617-633 at 484c164.

Raw partitions and IDs are pinned native execution. Lossless partitions use an
independent Unicode-point projection in JS and the same native hash managers.
Removing either V1 protection, shrinking a cut or carrying its growth into the
next cap must break the exact partition assertions, not just reconstruction.
"""
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from content_splitter import ContentSplitter
from tests.test_settings_matrix import matrix_vault, pieces_for

FIXTURE = json.loads(gzip.decompress(Path('tests/fixtures/lossless_upstream.json.gz').read_bytes()))
ROWS = FIXTURE['rows']


def assert_row(module, row):
    splitter = module.ContentSplitter(row['settings'])
    pieces = splitter.split(row['input'], path=row['path'])
    assert pieces == row['lossless_split_pieces']
    assert [splitter.chunk_id_for_piece(p) for p in pieces] == row['lossless_split_children']
    assert ''.join(pieces) == row['input']
    assert not any(0xD800 <= ord(c) <= 0xDFFF for p in pieces for c in p)


@pytest.mark.parametrize('row', ROWS, ids=lambda r:r['id'])
def test_exact_lossless_partition(row):
    import content_splitter
    assert_row(content_splitter, row)
    raw = ''.join(row['split_pieces']).encode('utf-16-le', 'surrogatepass')
    assert raw == row['input'].encode('utf-16-le')
    broken = any(0xD800 <= ord(c) <= 0xDFFF for p in row['split_pieces'] for c in p)
    if not broken:
        assert row['lossless_split_pieces'] == row['split_pieces']
        assert row['lossless_split_children'] == row['split_children']
    else:
        assert row['version'] == 'v1'
        assert row['lossless_split_pieces'] != row['split_pieces']


def test_lossless_provenance():
    from tests.settings_oracle import ORACLE
    for key, file in [('generator_sha256','scripts/extract_lossless_vectors.mjs'),
                      ('harness_sha256','scripts/extract_settings_oracle.mjs')]:
        assert FIXTURE[key] == hashlib.sha256(Path(file).read_bytes()).hexdigest()
    for key in ['upstream_commit','source_sha256','decoder_dependency','wasm_dependency']:
        assert FIXTURE['provenance'][key] == ORACLE[key]


@pytest.mark.parametrize('kind', ['couchdb','journal'])
@pytest.mark.parametrize('row', [r for r in ROWS if r['settings']['hashAlg']=='sha1'], ids=lambda r:r['id'])
async def test_lossless_backend_roundtrip(tmp_path, kind, row):
    # EntryManagerImpls.ts:280 preserves ordered children; journalRecords.ts:69-75
    # would replace isolated halves. A valid astral cut must produce no notice.
    settings = {'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False,**row['settings']}
    async with matrix_vault(tmp_path,kind,settings) as (backend,doc,chunks,log,storage):
        await backend.startup_check()
        result = await backend._write_content(row['path'],row['input'],len(row['input'].encode()),'plain',create=True,overwrite=False)
        assert not result.get('notices')
        assert not result.get('transport_replacements')
        metadata = await doc(row['path'])
        assert metadata['children'] == row['lossless_children']
        assert await pieces_for(backend,metadata['children']) == row['lossless_pieces']
        if kind == 'journal':
            from storage_journal import JournalBackend
            reader = JournalBackend(storage,tmp_path/'fresh')
        else:
            from storage_couchdb import CouchDBBackend
            reader = CouchDBBackend('http://test','user','password','obsidian',transport=backend._client._transport)
        try:
            await reader.startup_check()
            if kind == 'journal': await reader.refresh(force=True)
            assert (await reader.read_note(row['path']))['text'] == row['input']
        finally:
            await reader.close()


@pytest.mark.parametrize('row', FIXTURE['direct_rows'])
def test_lone_unit_does_not_extend_cut(row):
    from content_splitter import _capped, _unit_string
    # Native chunks.ts:243-252 has no pair to protect at these cuts.
    pieces = list(_capped(_unit_string(row['input']),row['cap']))
    assert pieces == row['lossless_pieces'] == row['pieces']
    splitter = ContentSplitter({'hashAlg':'sha1'})
    assert [splitter.chunk_id_for_piece(p) for p in pieces] == row['children'] == row['lossless_children']


WIRE = json.loads(Path('tests/fixtures/surrogate_wire_upstream.json').read_text())
LONE_INPUTS = [v for v in WIRE['vectors'] if v['name'] in ['start','middle','end','high','low']]


@pytest.mark.parametrize('kind',['couchdb','journal'])
@pytest.mark.parametrize('version',['v1','v2','v2-segmenter','v3-rabin-karp'])
@pytest.mark.parametrize('vector',LONE_INPUTS,ids=lambda v:v['name'])
async def test_caller_lone_unit_keeps_ingress_behaviour(tmp_path,kind,version,vector):
    # common/utils.ts:100-115 creates a Blob; its USVString conversion occurs
    # before chunks.ts. Native TextEncoder projection supplies the same values.
    settings = {'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False,
                'chunkSplitterVersion':version,'hashAlg':'sha1'}
    async with matrix_vault(tmp_path,kind,settings) as (backend,*_):
        await backend.startup_check()
        result = await backend.write_note('lone.md',vector['docs'][0]['data'],create=True)
        assert {'code':'input_surrogates_replaced','count':vector['transport_replacements']} in result['notices']
        assert not result.get('transport_replacements')
        assert not any(n['code']=='journal_surrogates_replaced' for n in result['notices'])
        assert (await backend.read_note('lone.md'))['text'] == vector['decoded'][0]['data']


async def test_historical_journal_cuts_are_not_repaired_on_read(tmp_path,monkeypatch):
    import base64
    from journal_codec import parse_journal
    from livesync_format import new_metadata_doc
    # journalRecords.ts:79-108 reads the already projected compact frames.
    # Retain this raw-plugin witness even though own writes no longer create it.
    wire = json.loads(Path('tests/fixtures/legacy_wire_cells_upstream.json').read_text())['rows'][0]
    parsed = parse_journal(base64.b64decode(wire['frame_base64']))
    settings = {'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False}
    async with matrix_vault(tmp_path,'journal',settings) as (backend,*_):
        await backend.startup_check()
        text = ''.join(wire['pieces'])
        doc = new_metadata_doc('historical.md',wire['children'],len(text.encode()),'plain',1,1)
        doc['_rev'] = '1-historical'
        backend._store.apply_journal('historical',parsed.chunks,[doc])
        def no_split(*a,**kw):
            pytest.fail('read must not split or repair stored children')
        monkeypatch.setattr(backend._splitter,'split',no_split)
        assert (await backend.read_note('historical.md'))['text'] == text
        assert backend._store.winner(backend._doc_id('historical.md'))['children'] == wire['children']
        assert await pieces_for(backend,wire['children']) == wire['pieces']


def test_all_settings_oracles_preserve_raw_or_repair_only_pairs():
    from tests.settings_oracle import ORACLE
    vectors = [v for row in ORACLE['matrix'] for v in row['payloads'].values()] + ORACLE['grid']
    for vector in vectors:
        raw = [ORACLE['pieces'][i] for i in vector['split_pieces']]
        lossless = [ORACLE['pieces'][i] for i in vector['lossless_split_pieces']]
        assert ''.join(raw).encode('utf-16-le','surrogatepass') == ''.join(lossless).encode('utf-16-le','surrogatepass')
        broken = any(a and b and 0xD800 <= ord(a[-1]) <= 0xDBFF
                     and 0xDC00 <= ord(b[0]) <= 0xDFFF for a,b in zip(raw,raw[1:]))
        if not broken:
            assert lossless == raw
            assert vector['lossless_split_children'] == vector['split_children']
        else:
            assert not any(a and b and 0xD800 <= ord(a[-1]) <= 0xDBFF
                           and 0xDC00 <= ord(b[0]) <= 0xDFFF
                           for a,b in zip(lossless,lossless[1:]))


def test_parameter_cells_keep_both_native_and_lossless_views():
    from tests.test_coercion_cells import ROWS as coercion
    from tests.test_legacy_cells import ALL_CELL_ROWS
    for row in coercion + ALL_CELL_ROWS:
        if row['status'] != 'defined':
            continue
        # These cells contain ASCII, empty text or BOM, so F13 changes no cut.
        assert row['lossless_split_pieces'] == row['split_pieces']
        assert row['lossless_split_children'] == row['split_children']
