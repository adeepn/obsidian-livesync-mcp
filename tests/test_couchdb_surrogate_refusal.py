"""CouchDB 3.5.3 live finding, 2026-11-25; chunks.ts:243-252,698-699."""
import json
from pathlib import Path

import httpx
import pytest

from storage_base import WriterParameterRefused
from storage_couchdb import CouchDBBackend
from tests.couchdb_fake import make_fake_couchdb
from content_splitter import ContentSplitter

W = json.loads(Path('tests/fixtures/surrogate_wire_upstream.json').read_text())['witness']


@pytest.mark.parametrize('route', ['leaf', '_local/state', '_bulk_docs'])
@pytest.mark.parametrize('value,accepted', [('\ud800', False), ('\udfff', False), ('🙂', True)])
async def test_fake_models_real_utf8_json(route, value, accepted):
    transport, _ = make_fake_couchdb({})
    async with httpx.AsyncClient(base_url='http://test', transport=transport) as client:
        doc = {'_id': 'leaf', 'data': value, 'type': 'leaf'}
        body = {'docs': [doc]} if route == '_bulk_docs' else doc
        response = await client.request('POST' if route == '_bulk_docs' else 'PUT', '/vault/' + route,
                                        content=json.dumps(body, ensure_ascii=True).encode())
        if accepted:
            assert response.status_code < 400
        else:
            assert response.status_code == 400
            assert response.json() == {'error': 'bad_request', 'reason': 'invalid UTF-8 JSON'}


async def test_whole_write_refuses_before_any_request():
    transport, log = make_fake_couchdb({})
    backend = CouchDBBackend('http://test', 'user', 'password', 'vault', transport=transport)
    backend._splitter = ContentSplitter(W['settings'])
    before = dict(backend._text_verdicts)
    try:
        with pytest.raises(WriterParameterRefused, match='CouchDB rejects.*unpaired UTF-16 surrogate') as error:
            await backend._write_content(W['path'], W['input'], len(W['input'].encode()), 'plain', create=True, overwrite=False)
        assert 'v3' in error.value.format()
        assert 'device' in error.value.format()
        assert not log
        assert backend._text_verdicts == before
    finally:
        await backend.close()


@pytest.mark.parametrize('body', [{'data': '\ud800'}, {'nested': [{'secret': '\udfff'}]}, {'\ud800': 'key'}])
@pytest.mark.parametrize('route', ['leaf', '_local/state'])
async def test_all_document_fields_refuse_before_request(body, route):
    transport, log = make_fake_couchdb({})
    backend = CouchDBBackend('http://test', 'user', 'password', 'vault', transport=transport)
    try:
        with pytest.raises(WriterParameterRefused):
            await backend._put_doc(route, body)
        assert not log
    finally:
        await backend.close()
