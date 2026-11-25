"""F6 transport policy, not wire oracles.

Upstream commonlib replication/journal/objectstore/MinioStorageAdapter.ts:70-80
selects four attempts; serviceFeatures/targetFilter.ts:132-149 has no deadline.
The peer retains single-attempt unconditional publication for outcome safety.
"""
from dataclasses import replace
import queue

import pytest

from config import ConfigError, load_config
from tests.test_config import COUCHDB_ENV, S3_ENV
from tests.test_object_storage import make_cfg

SECONDS = ('COUCHDB_CONNECT_TIMEOUT_SECONDS', 'COUCHDB_IO_TIMEOUT_SECONDS',
           'S3_CONNECT_TIMEOUT_SECONDS', 'S3_READ_TIMEOUT_SECONDS',
           'LIVESYNC_SELECTION_TIMEOUT_SECONDS')


@pytest.mark.parametrize('name', (*SECONDS, 'S3_MAX_ATTEMPTS'))
@pytest.mark.parametrize('value', ('0', '-1', 'nan', 'inf', '-inf', 'private-marker', '', None, True, [], {}))
def test_invalid_transport_values_are_value_free(name, value):
    env = dict(S3_ENV if name.startswith('S3_') else COUCHDB_ENV)
    env[name] = value
    with pytest.raises(ConfigError) as error:
        load_config(env)
    assert name in str(error.value)
    assert 'private-marker' not in str(error.value)


def test_fractional_attempts_refused():
    with pytest.raises(ConfigError, match='S3_MAX_ATTEMPTS'):
        load_config({**S3_ENV, 'S3_MAX_ATTEMPTS': '1.5'})


def test_transport_config_and_constructed_clients(monkeypatch):
    from object_storage import Boto3ObjectStorage
    from server import get_backend
    couch = load_config({**COUCHDB_ENV, 'COUCHDB_CONNECT_TIMEOUT_SECONDS': '11.5',
                         'COUCHDB_IO_TIMEOUT_SECONDS': '47',
                         'LIVESYNC_SELECTION_TIMEOUT_SECONDS': '17'})
    backend = get_backend(couch)
    timeout = backend._client.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (11.5, 47, 47, 47)
    backend._selection_init()
    assert backend._selection_worker.timeout == 17
    s3 = load_config({**S3_ENV, 'S3_CONNECT_TIMEOUT_SECONDS': '12',
                      'S3_READ_TIMEOUT_SECONDS': '48.5', 'S3_MAX_ATTEMPTS': '4'})
    configs = []
    def client(*args, **kwargs):
        configs.append(kwargs['config'])
        return object()
    monkeypatch.setattr('boto3.client', client)
    Boto3ObjectStorage(s3.s3)
    assert (configs[0].connect_timeout, configs[0].read_timeout) == (12, 48.5)
    assert configs[0].retries['total_max_attempts'] == 4


def test_selection_exchange_uses_instance_deadline():
    from selection_engine import SelectionWorker, EngineFailure
    worker = SelectionWorker(timeout=.125)
    worker._requests = queue.Queue()
    class Responses:
        def get(self, *, timeout):
            assert timeout == .125
            raise queue.Empty
    worker._responses = Responses()
    with pytest.raises(EngineFailure) as error:
        worker._exchange('evaluate', {})
    assert error.value.cause == 'timeout'


@pytest.mark.parametrize('operation', ('get', 'list', 'exists', 'put_if_absent', 'put'))
def test_dropped_response_retry_policy(operation, monkeypatch):
    """Exercise actual SDK retries after a server accepts but response is lost.

    Conditional repetition becomes 412; fallback PUT must remain ambiguous.
    Journal fallback consumer: storage_journal.py:_publish_mutation; upstream
    unconditional upload MinioStorageAdapter.ts:132-145 has no such protection.
    """
    from io import BytesIO
    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import ReadTimeoutError
    from object_storage import Boto3ObjectStorage
    from storage_base import OutcomeUnknown
    storage = Boto3ObjectStorage(make_cfg(max_attempts=4, custom_headers=(('X-F6-Test', 'synthetic'),)))
    calls = []
    class Raw(BytesIO):
        def stream(self, amt=None, decode_content=False):
            yield self.read()
    def send(request):
        calls.append(request)
        assert request.headers['X-F6-Test'] == b'synthetic'
        if len(calls) == 1:
            raise ReadTimeoutError(endpoint_url='http://synthetic.invalid', error='private-marker')
        if operation == 'put_if_absent':
            return AWSResponse(request.url, 412, {}, Raw(b'<Error><Code>PreconditionFailed</Code></Error>'))
        body = (b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
                if operation == 'list' else b'ok' if operation == 'get' else b'')
        return AWSResponse(request.url, 200, {'content-length': str(len(body))}, Raw(body))
    monkeypatch.setattr('botocore.endpoint.time.sleep', lambda seconds: None)
    for client in (storage._client, storage._write_client):
        monkeypatch.setattr(client._endpoint.http_session, 'send', send)
    if operation == 'put':
        with pytest.raises(OutcomeUnknown) as error:
            storage.put('journal', b'data')
        assert 'private-marker' not in str(error.value)
        assert len(calls) == 1
    else:
        result = getattr(storage, operation)('journal', b'data') if operation == 'put_if_absent' else getattr(storage, operation)('journal')
        assert result == {'get': b'ok', 'exists': True, 'list': [], 'put_if_absent': 'exists'}[operation]
        assert len(calls) == 2


def test_transport_defaults():
    c = load_config(COUCHDB_ENV)
    s = load_config(S3_ENV)
    assert (c.couchdb.connect_timeout_seconds, c.couchdb.io_timeout_seconds) == (5, 30)
    assert (s.s3.connect_timeout_seconds, s.s3.read_timeout_seconds, s.s3.max_attempts) == (5, 30, 1)
    assert c.selection_timeout_seconds == s.selection_timeout_seconds == 5


def test_selection_timeout_remedy_names_override():
    from selection_engine import EngineFailure
    message = str(EngineFailure('timeout'))
    assert 'LIVESYNC_SELECTION_TIMEOUT_SECONDS' in message
    assert 'restart' in message


def test_conditional_retry_keeps_prior_uncertainty(monkeypatch):
    from botocore.exceptions import ReadTimeoutError, EndpointConnectionError
    from object_storage import Boto3ObjectStorage
    from storage_base import OutcomeUnknown
    storage = Boto3ObjectStorage(make_cfg(max_attempts=3))
    calls = []
    def send(request):
        calls.append(request)
        if len(calls) == 1:
            # Server accepted creation, but the response never reached the SDK.
            raise ReadTimeoutError(endpoint_url='http://synthetic.invalid')
        raise EndpointConnectionError(endpoint_url='http://synthetic.invalid')
    monkeypatch.setattr('botocore.endpoint.time.sleep', lambda seconds: None)
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', send)
    with pytest.raises(OutcomeUnknown):
        storage.put_if_absent('journal', b'data')
    assert len(calls) == 3


def test_retried_unsupported_conditional_keeps_uncertainty(monkeypatch):
    from io import BytesIO
    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import ReadTimeoutError
    from object_storage import Boto3ObjectStorage
    from storage_base import OutcomeUnknown
    storage = Boto3ObjectStorage(make_cfg(max_attempts=3))
    calls = []
    class Raw(BytesIO):
        def stream(self, amt=None, decode_content=False):
            yield self.read()
    def send(request):
        calls.append(request)
        if len(calls) == 1:
            raise ReadTimeoutError(endpoint_url='http://synthetic.invalid')
        return AWSResponse(request.url, 501, {}, Raw(b'<Error><Code>NotImplemented</Code></Error>'))
    monkeypatch.setattr('botocore.endpoint.time.sleep', lambda seconds: None)
    monkeypatch.setattr(storage._client._endpoint.http_session, 'send', send)
    with pytest.raises(OutcomeUnknown):
        storage.put_if_absent('journal', b'data')
    assert len(calls) == 2
