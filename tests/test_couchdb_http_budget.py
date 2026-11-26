"""Transport defence, not a plugin decoding oracle.

C:services/base/RemoteService.ts:340-380 delegates to fetch/observeResponseBody;
C:replication/couchdb/LiveSyncReplicator.ts:2071-2098 supplies custom headers.
The peer deliberately bounds hostile HTTP bodies before JSON/document decoding.
Synthetic RFC 1950/1952 messages exercise budgets, not LiveSync expected values.
"""
import gzip
import zlib

import httpx
import pytest

from couchdb_compression import CouchDBInflateBudgetExceeded
from storage_base import StorageError
from storage_couchdb import CouchDBBackend

LIMIT = 4096
PATHS = [('GET', 'vault/h:leaf'), ('GET', 'vault/_all_docs'),
         ('POST', 'vault/_bulk_docs'), ('GET', 'vault/_local/milestone'),
         ('GET', 'vault/note/attachment'), ('GET', '_session'),
         ('GET', 'vault'), ('GET', 'vault/_changes')]


class ProbeStream(httpx.AsyncByteStream):
    def __init__(self, wire, *, block=31, fail_after=None):
        self.wire, self.block, self.fail_after = wire, block, fail_after
        self.seen = 0
        self.closed = False

    async def __aiter__(self):
        for start in range(0, len(self.wire), self.block):
            self.seen += 1
            if self.fail_after is not None and self.seen > self.fail_after:
                pytest.fail('body buffered before enforcing budget')
            yield self.wire[start:start + self.block]

    async def aclose(self):
        self.closed = True


def pack(data, encoding):
    if encoding == 'gzip':
        return gzip.compress(data, mtime=0)
    if encoding == 'deflate':
        return zlib.compress(data)
    return data


def backend(wire, encoding, **stream_options):
    stream = ProbeStream(wire, **stream_options)
    def send(request):
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, headers={'Content-Encoding': encoding}, stream=stream)
    peer = CouchDBBackend('https://db.example', 'u', 'p', 'vault',
                         transport=httpx.MockTransport(send))
    peer.couchdb_max_inflated_bytes = LIMIT
    return peer, stream


@pytest.mark.parametrize('method,path', PATHS)
@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
@pytest.mark.parametrize('extra', [0, 1])
async def test_every_response_enforces_decoded_boundary(method, path, encoding, extra):
    data = b'a' * (LIMIT + extra)
    peer, stream = backend(pack(data, encoding), encoding)
    try:
        if extra:
            with pytest.raises(CouchDBInflateBudgetExceeded) as caught:
                await peer._request(method, path)
            assert 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' in caught.value.format()
            assert 'restart' in caught.value.format()
        else:
            assert (await peer._request(method, path)).content == data
        assert stream.closed
    finally:
        await peer.close()


@pytest.mark.parametrize('method,path', PATHS)
@pytest.mark.parametrize('extra', [0, 1])
async def test_raw_cap_independent_of_decoded_cap(method, path, extra):
    small = gzip.compress(b'ok', mtime=0)
    # RFC1952 optional zero-terminated filename: wire large, decoded tiny.
    wire = small[:3] + bytes([small[3] | 8]) + small[4:10]
    wire += b'x' * (LIMIT + extra - len(small) - 1) + b'\0' + small[10:]
    peer, stream = backend(wire, 'gzip')
    try:
        if extra:
            with pytest.raises(CouchDBInflateBudgetExceeded):
                await peer._request(method, path)
        else:
            assert (await peer._request(method, path)).content == b'ok'
        assert stream.closed
    finally:
        await peer.close()


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
async def test_inflation_stops_before_next_wire_block(encoding):
    peer, stream = backend(pack(b'a' * (16 * 1024**2), encoding), encoding,
                           block=1024, fail_after=1)
    try:
        with pytest.raises(CouchDBInflateBudgetExceeded):
            await peer._request('GET', 'vault/h:leaf')
        assert stream.seen == 1
        assert stream.closed
    finally:
        await peer.close()


@pytest.mark.parametrize('encoding', ['secret-encoding', 'br', 'gzip, deflate'])
async def test_unknown_encoding_is_typed_and_value_free(encoding):
    peer, stream = backend(b'secret-body', encoding)
    try:
        with pytest.raises(StorageError) as caught:
            await peer._request('GET', 'vault/h:leaf')
        message = caught.value.format()
        assert 'Content-Encoding' in message
        assert 'identity' in message
        assert 'secret' not in message
        assert stream.seen == 0
        assert stream.closed
    finally:
        await peer.close()


async def test_custom_encoding_cannot_override_identity():
    peer, _ = backend(b'ok', 'identity')
    peer._custom_headers = {'Accept-Encoding': 'gzip'}
    try:
        assert (await peer._request('GET', 'vault')).content == b'ok'
    finally:
        await peer.close()


async def test_batch_documents_have_per_document_headroom():
    data = b'a' * (LIMIT * 2)
    peer, _ = backend(pack(data, 'gzip'), 'gzip')
    try:
        response = await peer._request('POST', 'vault/_all_docs',
                                       json_body={'keys': ['a', 'b'], 'include_docs': True})
        assert response.content == data
    finally:
        await peer.close()


@pytest.mark.parametrize('encoding', ['gzip', 'deflate', 'raw-deflate'])
@pytest.mark.parametrize('block', [1, 2, 3, 1024])
async def test_fragmented_compressed_bodies(encoding, block):
    data = b'hello' * 100
    if encoding == 'raw-deflate':
        compressor = zlib.compressobj(wbits=-15)
        wire = compressor.compress(data) + compressor.flush()
    else:
        wire = pack(data, encoding)
    peer, _ = backend(wire, 'deflate' if encoding == 'raw-deflate' else encoding, block=block)
    try:
        assert (await peer._request('GET', 'vault/h:leaf')).content == data
    finally:
        await peer.close()


async def test_concatenated_gzip_members_share_decoded_budget():
    wire = gzip.compress(b'a' * LIMIT) + gzip.compress(b'b')
    peer, _ = backend(wire, 'gzip', block=1)
    try:
        with pytest.raises(CouchDBInflateBudgetExceeded):
            await peer._request('GET', 'vault/h:leaf')
    finally:
        await peer.close()


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
async def test_truncated_encoding_is_typed(encoding):
    peer, stream = backend(pack(b'hello', encoding)[:-1], encoding)
    try:
        with pytest.raises(StorageError) as caught:
            await peer._request('PUT', 'vault/h:leaf', json_body={'data': 'hello'})
        assert 'identity' in caught.value.format()
        assert 're-read' in caught.value.format()
        assert stream.closed
    finally:
        await peer.close()


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
async def test_oversized_write_acknowledgement_requires_confirmation(encoding):
    peer, _ = backend(pack(b'a' * (LIMIT + 1), encoding), encoding)
    try:
        with pytest.raises(CouchDBInflateBudgetExceeded) as caught:
            await peer._request('PUT', 'vault/h:leaf', json_body={'data': 'hello'})
        assert 're-read' in caught.value.format()
    finally:
        await peer.close()


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
async def test_default_splitter_maximum_document_and_attachment(encoding):
    import base64
    import json
    # ContentSplitterBase.ts:68; chunks.ts:527-545,617-633: V3 cap 4 MiB+3.
    # Exercise actual JSON/base64 envelope, plus direct attachment transport.
    attachment = b'a' * (4 * 1024**2 + 3)
    doc = {'_id': 'h:leaf', 'type': 'leaf',
           'data': base64.b64encode(attachment).decode('ascii')}
    for path, data in [('vault/h:leaf', json.dumps(doc).encode()),
                       ('vault/note/attachment', attachment)]:
        peer, _ = backend(pack(data, encoding), encoding, block=65536)
        peer.couchdb_max_inflated_bytes = None
        try:
            response = await peer._request('GET', path)
            assert response.content == data
            if path.endswith('h:leaf'):
                assert base64.b64decode(response.json()['data']) == attachment
        finally:
            await peer.close()


@pytest.mark.parametrize('method,path', PATHS)
@pytest.mark.parametrize('extra', [0, 1])
async def test_deflate_raw_boundary_below_decoded_cap(method, path, extra):
    data = b'a' * (LIMIT - 11 + extra)
    wire = zlib.compress(data, level=0)  # RFC1950 + one stored RFC1951 block
    assert len(wire) == LIMIT + extra
    peer, stream = backend(wire, 'deflate')
    try:
        if extra:
            with pytest.raises(CouchDBInflateBudgetExceeded):
                await peer._request(method, path)
        else:
            assert (await peer._request(method, path)).content == data
        assert stream.closed
    finally:
        await peer.close()
