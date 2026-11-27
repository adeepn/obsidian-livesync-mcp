import json
import pytest
import server
from tests.fake_backend import FakeBackend


async def test_vault_stats_exposes_assessment_only(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, 'backend', backend)
    async def info():
        return {'backend': 'fake', 'tweak_assessment': {
            'status': 'LOSSY_AUTO_ACCEPTABLE', 'differing_keys': ['hashAlg'],
            'values': {'password': 'PASS-MARKER'}}}
    monkeypatch.setattr(backend, 'backend_info', info)
    result = json.loads(await server.obsidian_vault_stats())
    assert result['tweak_assessment'] == {
        'status': 'LOSSY_AUTO_ACCEPTABLE', 'differing_keys': ['hashAlg']}
    assert 'PASS-MARKER' not in json.dumps(result)


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_real_backend_assessment_reaches_vault_stats(tmp_path, kind, monkeypatch):
    from tests.test_tweak_startup import vault
    from tests.test_tweak_compatibility import FULL
    async with vault(tmp_path, kind, {'tweak_values': {'PREFERRED': FULL}}) as (backend, _):
        await backend.startup_check()
        monkeypatch.setattr(server, 'backend', backend)
        result = json.loads(await server.obsidian_vault_stats())
        assert result['tweak_assessment'] == {'status': 'OK', 'differing_keys': []}
