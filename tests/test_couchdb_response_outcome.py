"""Committed acknowledgements can fail independently of publication.

C:services/base/RemoteService.ts:135-159,373-381 observes body-reader failures
AFTER fetch settles. These synthetic transport faults test the peer's outcome
contract, not a plugin format oracle; no upstream expected bytes are invented.
"""
import gzip

import httpx
import pytest

from storage_base import MutationNotApplied, OutcomeUnknown, StorageUnavailable
from storage_couchdb import CouchDBHTTPBudgetExceeded, CouchDBHTTPEncodingRefused
from tests.test_couchdb_http_budget import LIMIT, ProbeStream, backend
from tests.test_storage_couchdb import fake_backend

KINDS = ('budget', 'truncated', 'unknown', 'corrupt', 'dropped')


class DroppedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{'
        raise httpx.ReadError('PRIVATE_REMOTE_BODY')


def broken_ack(kind, request):
    if kind == 'dropped':
        return httpx.Response(201, stream=DroppedStream(), request=request)
    wire = (gzip.compress(b'x' * (LIMIT + 1), mtime=0) if kind == 'budget'
            else b'corrupt' if kind == 'corrupt' else gzip.compress(b'{}', mtime=0)[:-1])
    return httpx.Response(201, headers={'Content-Encoding': 'PRIVATE' if kind == 'unknown' else 'gzip'},
                          stream=ProbeStream(wire), request=request)


def intercept(monkeypatch, peer, kind, method, suffix):
    original = peer._client._transport.handle_async_request

    async def send(request):
        response = await original(request)  # commit first; replace acknowledgement only
        if request.method == method and request.url.path.endswith(suffix):
            assert response.status_code < 300
            return broken_ack(kind, request)
        return response

    monkeypatch.setattr(peer._client._transport, 'handle_async_request', send)
    peer.couchdb_max_inflated_bytes = LIMIT


def assert_recovery(error, kind):
    text = error.message('append')
    assert 'MAY have been applied' in text and 'NON-IDEMPOTENT' in text
    assert 're-read' in text and 'PRIVATE' not in text
    if kind == 'budget':
        assert str(LIMIT) in text
        assert 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' in text and 'restart' in text
    elif kind != 'dropped':
        assert 'Content-Encoding' in text and 'identity' in text
    else:
        assert 'ReadError' in text


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('endpoint', ('document', 'bulk', 'local', 'delete', 'leaf'))
async def test_committed_ack_is_unknown(kind, endpoint, monkeypatch):
    async with fake_backend() as (peer, _):
        await peer.write_note('only.md', 'old', create=True)
        old = await peer._get_doc('only.md')
        method = 'DELETE' if endpoint == 'delete' else 'POST' if endpoint == 'bulk' else 'PUT'
        doc_id = '_local/checkpoint' if endpoint == 'local' else 'h:leaf' if endpoint == 'leaf' else 'only.md'
        suffix = '/_bulk_docs' if endpoint == 'bulk' else '/' + doc_id
        with monkeypatch.context() as patch:
            intercept(patch, peer, kind, method, suffix)
            with pytest.raises(OutcomeUnknown) as caught:
                if endpoint == 'document':
                    await peer.write_note('only.md', 'new')
                elif endpoint == 'bulk':
                    await peer._request(method, peer._db_path + suffix,
                                        json_body={'docs': [{**old, 'data': 'new'}]})
                elif endpoint == 'delete':
                    await peer._request(method, peer._db_path + suffix, params={'rev': old['_rev']})
                else:
                    await peer._put_doc(doc_id, {'data': 'new', 'type': 'leaf'})
        # Read-back proves publication despite the exception, including the leaf.
        published = await peer._get_doc(doc_id)
        if endpoint == 'delete':
            assert published is None
        else:
            if endpoint == 'document':
                assert (await peer.read_note(doc_id))['text'] == 'new'
            else:
                assert published['data'] == 'new'
            assert published['_rev'] != old['_rev'] or doc_id != 'only.md'
        assert_recovery(caught.value, kind)


@pytest.mark.parametrize('kind', KINDS)
async def test_append_retains_public_guidance(kind, monkeypatch):
    import server
    async with fake_backend() as (peer, _):
        await peer.write_note('only.md', 'old', create=True)
        old_rev = (await peer.get_meta('only.md'))['rev']
        with monkeypatch.context() as patch:
            intercept(patch, peer, kind, 'PUT', '/only.md')
            patch.setattr(server, 'backend', peer)
            result = await server.obsidian_update_note('only.md', 'append', 'extra')
        assert 'extra' in (await peer.read_note('only.md'))['text']
        assert (await peer.get_meta('only.md'))['rev'] != old_rev
        print(f'{kind}: published=True; {result}')
        assert 'MAY have been applied' in result and 'NON-IDEMPOTENT' in result
        assert 're-read' in result and 'PRIVATE' not in result
        if kind == 'budget':
            assert 'LIVESYNC_COUCHDB_MAX_INFLATED_BYTES' in result
        elif kind != 'dropped':
            assert 'identity' in result


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('method', ('GET', 'HEAD'))
async def test_read_body_refusal_stays_typed(kind, method):
    response = broken_ack(kind, httpx.Request(method, 'https://db.example/vault/a'))
    peer, _ = backend(b'', 'identity')
    peer._client._transport = httpx.MockTransport(lambda request: response)
    expected = (CouchDBHTTPBudgetExceeded if kind == 'budget' else
                StorageUnavailable if kind == 'dropped' else CouchDBHTTPEncodingRefused)
    try:
        with pytest.raises(expected) as caught:
            await peer._request(method, 'vault/a')
        assert not isinstance(caught.value, OutcomeUnknown)
    finally:
        await peer.close()


@pytest.mark.parametrize('kind', KINDS)
async def test_orphan_leaf_does_not_publish_note(kind, monkeypatch):
    async with fake_backend() as (peer, _):
        original = peer._client._transport.handle_async_request
        leaf_ids = []

        async def send(request):
            response = await original(request)
            if request.method == 'PUT' and '/h:' in request.url.path:
                leaf_ids.append(request.url.path.rsplit('/', 1)[1])
                return broken_ack(kind, request)
            return response

        with monkeypatch.context() as patch:
            patch.setattr(peer._client._transport, 'handle_async_request', send)
            peer.couchdb_max_inflated_bytes = LIMIT
            with pytest.raises(MutationNotApplied) as caught:
                await peer._write_chunk('only.md', 'leaf-data')
        assert await peer._get_doc('only.md') is None
        assert leaf_ids and await peer._get_doc(leaf_ids[0]) is not None
        assert 'orphan' in str(caught.value)


@pytest.mark.parametrize('method', ('GET', 'HEAD', 'PUT', 'POST', 'DELETE'))
@pytest.mark.parametrize('error', (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                                 httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
async def test_transport_send_boundary(method, error):
    peer, _ = backend(b'', 'identity')

    def send(request):
        raise error('PRIVATE_REMOTE_BODY')

    peer._client._transport = httpx.MockTransport(send)
    try:
        with pytest.raises(StorageUnavailable) as caught:
            await peer._request(method, 'vault/a')
        uncertain = method in {'PUT', 'POST', 'DELETE'} and error not in {
            httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout}
        assert isinstance(caught.value, OutcomeUnknown) == uncertain
        assert 'PRIVATE' not in caught.value.format()
    finally:
        await peer.close()


@pytest.mark.parametrize('kind', KINDS)
async def test_move_source_removal_retains_recovery(kind, monkeypatch):
    async with fake_backend() as (peer, _):
        await peer.write_note('only.md', 'old', create=True)
        with monkeypatch.context() as patch:
            intercept(patch, peer, kind, 'PUT', '/only.md')
            with pytest.raises(OutcomeUnknown) as caught:
                await peer.rename_note('only.md', 'moved.md')
        assert await peer.read_note('only.md') is None
        assert (await peer.read_note('moved.md'))['text'] == 'old'
        assert caught.value.source_removal
        assert 'destination published; source removal unknown' in caught.value.message()
        assert_recovery(caught.value, kind)
