"""Journal uncertainty contract; not a plugin wire-format oracle.

Pinned commonlib MinioStorageAdapter.ts:70-80,132-145 retries uploads.
The peer must prove an ambiguous attempt at its original immutable key.
"""
from io import BytesIO
from urllib.parse import urlsplit, unquote

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ReadTimeoutError

from object_storage import Boto3ObjectStorage
from storage_base import OutcomeUnknown, StorageUnavailable
from tests.test_object_storage import make_cfg
from tests.test_journal_security import backend


@pytest.mark.parametrize('mode', ['identical', 'absent', 'different', 'equivalent', 'unreadable'])
async def test_lost_conditional_response_one_publication(tmp_path, monkeypatch, mode):
    peer, storage = await backend(tmp_path)
    sdk = Boto3ObjectStorage(make_cfg(max_attempts=4, prefix=''))
    calls, accepted = [], []

    class Raw(BytesIO):
        def stream(self, amt=None, decode_content=False):
            yield self.read()

    def send(request):
        key = unquote(urlsplit(request.url).path.rsplit('/', 1)[-1])
        calls.append(key)
        if len(calls) > 1:
            return AWSResponse(request.url, 412, {},
                Raw(b'<Error><Code>PreconditionFailed</Code></Error>'))
        body = request.body.read() if hasattr(request.body, 'read') else request.body
        storage._objects[key] = body
        accepted.append(key)
        if mode == 'absent':
            del storage._objects[key]
        if mode == 'different':
            storage._objects[key] = b'different'
        if mode == 'equivalent':
            import zlib
            # Same decoded records are insufficient: require exact object bytes.
            compressor = zlib.compressobj(0, zlib.DEFLATED, -15)
            storage._objects[key] = compressor.compress(zlib.decompress(body, -15)) + compressor.flush()
            assert storage._objects[key] != body
        raise ReadTimeoutError(endpoint_url='http://synthetic.invalid')

    monkeypatch.setattr(sdk._client._endpoint.http_session, 'send', send)
    monkeypatch.setattr('botocore.endpoint.time.sleep', lambda _: None)
    original_get = storage.get

    def readback(key):
        if mode == 'unreadable':
            raise StorageUnavailable()
        return original_get(key)

    sdk.get = readback
    storage.put_if_absent = sdk.put_if_absent
    try:
        if mode != 'identical':
            with pytest.raises(OutcomeUnknown):
                await peer.write_note('lost.md', 'synthetic payload', create=True)
            assert len(peer._store.outbound_journals()) == 1
            assert peer._store.outbound_journals()[0][2] == 0
            assert peer._store.last_sent_key == ''
        else:
            await peer.write_note('lost.md', 'synthetic payload', create=True)
            assert (await peer.read_note('lost.md'))['text'] == 'synthetic payload'
            assert peer._store.outbound_journals()[0][2] == 1
            assert peer._store.last_sent_key == accepted[0]
        assert len(accepted) == 1
        assert calls == [accepted[0], accepted[0]]
        assert len([key for key in storage._objects if key in accepted]) == (0 if mode == 'absent' else 1)
    finally:
        await peer.close()
        sdk._client.close()
        sdk._write_client.close()
