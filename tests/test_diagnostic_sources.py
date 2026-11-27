import pytest
import server


async def test_real_couchdb_writer_does_not_reflect_setting_objects(monkeypatch):
    from tests.test_storage_couchdb import fake_vault
    milestone = {'tweak_values': {'PREFERRED': {'hashAlg': {
        'aws_access_key_id': 'AK-MARKER', 'password': 'PASS-MARKER'}}}}
    async with fake_vault({'_local/obsydian_livesync_milestone': milestone}) as (backend, _, log):
        await backend.startup_check()
        monkeypatch.setattr(server, 'backend', backend)
        result = await server.obsidian_create_note('note.md', 'body')
        assert 'AK-MARKER' not in result and 'PASS-MARKER' not in result
        assert 'hashAlg=<invalid: object>' in result
        assert not [r for r in log if r.method == 'PUT']


@pytest.mark.parametrize('key', ['hashAlg', 'chunkSplitterVersion', 'idDerivationVersion'])
def test_writer_setting_values_never_enter_exceptions(key):
    from content_splitter import ContentSplitter
    class Hostile:
        def __str__(self):
            raise AssertionError('setting value was stringified')
        def __repr__(self):
            raise AssertionError('setting value was repr-ed')
    with pytest.raises(ValueError) as caught:
        ContentSplitter({key: Hostile()})
    assert f'{key}=<invalid: object>' in str(caught.value)


def test_path_normalization_value_never_enters_exception():
    from livesync_format import resolve_path_identity
    with pytest.raises(ValueError) as caught:
        resolve_path_identity({'tweak_values': {'PREFERRED': {
            'pathNormalizationVersion': {'password': 'PASS-MARKER'}}}})
    assert 'PASS-MARKER' not in str(caught.value)


def test_transport_failure_does_not_reflect_bucket_configuration(caplog):
    from object_storage import Boto3ObjectStorage
    adapter = Boto3ObjectStorage.__new__(Boto3ObjectStorage)
    adapter._bucket = {'password': 'PASS-MARKER'}
    error = adapter._unavailable('GET', 'journal-key', OSError('remote body'))
    assert 'PASS-MARKER' not in str(error)
    assert 'PASS-MARKER' not in caplog.text
    assert 'GET' in str(error) and 'OSError' in str(error)
    assert 'journal-key' not in str(error)
