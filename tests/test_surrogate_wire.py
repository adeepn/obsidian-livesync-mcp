"""Pinned journalRecords.ts:69-108; chunks.ts:243-252; PouchDB adapter 9.0.0 lib/index.es.js:772."""
import base64
import json
from pathlib import Path

import pytest

from content_splitter import ContentSplitter
from journal_codec import serialize_journal, parse_journal, deflate_journal, inflate_journal
from storage_couchdb import CouchDBBackend
from storage_journal import JournalBackend
from tests.couchdb_fake import make_fake_couchdb
from tests.test_storage_journal import write_backend

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/surrogate_wire_upstream.json').read_text())
W = FIXTURE['witness']
REPLACEMENTS = FIXTURE['vectors'][0]['transport_replacements']


@pytest.mark.parametrize('v', FIXTURE['vectors'], ids=lambda v: v['name'])
def test_journal_upstream_wire(v):
    chunks = [(d['_id'], d['data']) for d in v['docs'] if d['_id'].startswith('h:')]
    docs = [d for d in v['docs'] if not d['_id'].startswith('h:')]
    expected = base64.b64decode(v['frame_b64'])
    actual = serialize_journal(chunks, docs)
    assert actual == expected
    assert inflate_journal(deflate_journal(actual)) == expected
    parsed = parse_journal(expected)
    assert parsed.chunks == {d['_id']: d['data'] for d in v['decoded'] if d['_id'].startswith('h:')}
    assert parsed.docs == [d for d in v['decoded'] if not d['_id'].startswith('h:')]


@pytest.mark.parametrize('v', FIXTURE['vectors'], ids=lambda v: v['name'])
async def test_couchdb_upstream_wire(v):
    transport, log = make_fake_couchdb({d['_id']: d for d in v['docs'] if d.get('_rev')})
    backend = CouchDBBackend('http://test', 'user', 'password', 'vault', transport=transport)
    try:
        for doc, expected in zip(v['docs'], v['couchdb_json']):
            await backend._request('PUT', backend._doc_path(doc['_id']), json_body=doc)
            request = log[-1]
            assert request.content.decode('utf-8') == expected
            assert request.extensions['raw_body'] == request.content
            assert request.headers['content-type'] == 'application/json'
            response = await backend._request('GET', backend._doc_path(doc['_id']))
            stored = response.json()
            for k, value in doc.items():
                if k != '_rev':
                    assert stored[k] == value
    finally:
        await backend.close()


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_witness_publication_and_fresh_reader(tmp_path, kind):
    if kind == 'journal':
        backend, storage = await write_backend(tmp_path)
    else:
        transport, log = make_fake_couchdb({})
        backend = CouchDBBackend('http://test', 'user', 'password', 'vault', transport=transport)
    backend._splitter = ContentSplitter(W['settings'])
    assert list(backend._splitter.split(W['input'], path=W['path'])) == W['pieces']
    try:
        # Programmatic text/plain disk-ingestion representation; case-sensitive .MD split hint.
        result = await backend._write_content(W['path'], W['input'], len(W['input'].encode()), 'plain', create=True, overwrite=False)
        if kind == 'journal':
            assert type(result['transport_replacements']) is int
            assert result['transport_replacements'] == REPLACEMENTS
            stored = backend._store.winner(backend._doc_id(W['path']))
            reader = JournalBackend(storage, tmp_path / 'reader')
            await reader.startup_check()
            await reader.refresh()
            leaves = [reader._store._chunk_path(c).read_text(encoding='utf-8') for c in W['ids']]
            expected_pieces = [d['data'] for d in FIXTURE['vectors'][0]['decoded']]
            assert leaves == expected_pieces
            expected = ''.join(expected_pieces)
        else:
            assert 'transport_replacements' not in result
            stored = (await backend._get_doc(result['doc_id']))
            reader = CouchDBBackend('http://test', 'user', 'password', 'vault', transport=transport)
            leaves = [(await reader._get_doc(c))['data'] for c in W['ids']]
            assert leaves == W['pieces']
            expected = W['input']
        assert stored['children'] == W['ids']
        try:
            note = await reader.read_note(W['path'])
            assert note['text'] == expected
            repeated = await backend._write_content(
                W['path'], W['input'], len(W['input'].encode()), 'plain',
                create=False, overwrite=False, expected_rev=result['rev'])
            if kind == 'journal':
                assert repeated['transport_replacements'] == REPLACEMENTS
        finally:
            await reader.close()
    finally:
        await backend.close()


@pytest.mark.parametrize('structured', [False, True])
async def test_tool_reports_only_typed_transport_count(tmp_path, structured):
    import server
    backend, _ = await write_backend(tmp_path)
    backend._splitter = ContentSplitter(W['settings'])
    token = server._active_backend.set(backend)

    @server._tool_errors(writer=True)
    async def publish():
        await backend._write_content(W['path'], W['input'], len(W['input'].encode()), 'plain', create=True, overwrite=False)
        return '{"status":"ok"}' if structured else 'Created.'

    try:
        output = await publish()
        sentence = ('The LiveSync journal transport replaces unpaired surrogate units with U+FFFD; '
                    'the note is stored exactly as devices would store it.')
        assert sentence in output
        assert W['input'] not in output
        assert all(piece not in output for piece in W['pieces'])
        if structured:
            parsed = json.loads(output)
            assert type(parsed['transport_replacements']) is int
            assert parsed['transport_replacements'] == REPLACEMENTS
            assert parsed == {'status': 'ok', 'transport_replacements': REPLACEMENTS,
                              'message': sentence}
        else:
            assert output == 'Created.\n' + sentence
        # A subsequent unrelated tool invocation must not inherit the count.
        @server._tool_errors(writer=True)
        async def ordinary():
            return 'Unchanged.'
        assert await ordinary() == 'Unchanged.'
    finally:
        server._active_backend.reset(token)
        await backend.close()


async def test_metadata_publication_preserves_surrogates(tmp_path):
    backend, _ = await write_backend(tmp_path)
    try:
        doc = {'_id': 'metadata.md', '_rev': '1-test', 'path': 'metadata.md',
               'type': 'plain', 'children': [], 'value': next(v for v in FIXTURE['vectors'] if v['name'] == 'metadata-start')['docs'][0]['value']}
        assert backend._publish([], [doc]) == 0
        assert backend._store.winner('metadata.md')['value'] == doc['value']
    finally:
        await backend.close()


@pytest.mark.parametrize('control', FIXTURE['splitter_controls'], ids=lambda c: c['version'])
def test_v2_protects_pairs_unlike_v1_witness(control):
    # commonlib chunks.ts:307-310 and :362 protect V2 cuts, contrary to task premise.
    splitter = ContentSplitter({**W['settings'], 'chunkSplitterVersion': control['version']})
    assert list(splitter.split(W['input'], path=W['path'])) == control['pieces']
