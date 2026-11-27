"""P-18: injected secrets must never cross the MCP diagnostic boundary."""
import inspect
import json
from urllib.parse import quote

import pytest
import server
import storage_base as errors
from tests.fake_backend import FakeBackend
from tests.test_storage_messages import descendants

LABELS = ['aws_access_key_id', 'aws_secret_access_key', 'aws_session_token',
          'db_password', 'dbPassword', 'access_key', 'secret', 'secret_key', 'password',
          'passwd', 'passphrase', 'token', 'api_key', 'apikey', 'client_secret',
          'private_key', 'idDerivationKey', 'id_derivation_key']
PAIRS = [(f'{label}=Credential-Marker', 'Credential-Marker') for label in LABELS]
PAIRS += [(json.dumps({label.upper(): 'Credential-Marker'}), 'Credential-Marker') for label in LABELS]
PAIRS += [(repr({label: 'Credential-Marker'}), 'Credential-Marker') for label in LABELS]
PAIRS += [
    ('Authorization: Custom Credential-Marker extra', 'Credential-Marker'),
    ('Authorization:\n Credential-Marker', 'Credential-Marker'),
    ('password={"nested": {"value": "Credential-Marker"}}', 'Credential-Marker'),
    ('token=["Credential-Marker", "second"]', 'Credential-Marker'),
    (json.dumps({'reason': json.dumps({'password': 'Credential-Marker'})}), 'Credential-Marker'),
    ('passphrase=two words Credential-Marker', 'Credential-Marker'),
    ('bEaReR Credential-Marker', 'Credential-Marker'),
    ('bAsIc Credential-Marker', 'Credential-Marker'),
    ('http://ak:sk@host', 'ak:sk'),
    ('http://user:%3CCredential-Marker%3E@host', 'Credential-Marker'),
    ('http://user:%20Credential-Marker@host', 'Credential-Marker'),
    ('http://u%40name:%20Credential-Marker@host', 'Credential-Marker'),
    ('http://user:%0ACredential-Marker@host', 'Credential-Marker'),
    ('custom://user:Credential-Marker@host/path', 'Credential-Marker'),
    ('password=123456789012345678901234567890', '123456789012345678901234567890'),
    ('aws-access-key-id : Credential-Marker', 'Credential-Marker'),
    ('secret.key = Credential-Marker', 'Credential-Marker'),
    ('0123456789abcdef' * 2, '0123456789abcdef' * 2),
    ('aB9+' * 12, 'aB9+' * 12),
    ('AB9/' * 12, 'AB9/' * 12),
    ('ab9+' * 12, 'ab9+' * 12),
    ('AKIA0123456789ABCDEF', 'AKIA0123456789ABCDEF'),
    ('ASIA0123456789ABCDEF', 'ASIA0123456789ABCDEF'),
]
CORPUS = [(f'safe cause ({payload}); trailing context', secret) for payload, secret in PAIRS]
CORPUS += [(f'safe cause {quote(payload, safe="")}', secret) for payload, secret in PAIRS]


@pytest.mark.parametrize('payload,secret', CORPUS)
def test_sanitizer_adversarial_corpus(payload, secret):
    result = server._safe_storage_detail(errors.StorageError(payload))
    assert secret not in result
    assert quote(secret, safe='') not in result
    assert result == 'storage operation failed'


def injected(kind, payload):
    error = kind.__new__(kind)
    Exception.__init__(error, payload)
    error.__dict__.update(path=payload, size=1, maximum=2, source=payload,
                          destination=payload, source_state='live', detail=payload)
    return error


@pytest.mark.parametrize('kind', descendants(errors.StorageError))
@pytest.mark.parametrize('payload,secret', CORPUS)
async def test_all_tool_outcomes_hide_injected_secrets(monkeypatch, kind, payload, secret):
    backend = FakeBackend()
    monkeypatch.setattr(server, 'backend', backend)
    backend.errors['refresh'] = injected(kind, payload)
    for handler in server._tool_handlers:
        signature = inspect.signature(handler)
        kwargs = {name: 'note.md' for name, param in signature.parameters.items()
                  if param.default is inspect.Parameter.empty}
        # Include read and mutation branches of multi-action tools.
        actions = {'obsidian_manage_properties': ['get', 'set', 'delete'],
                   'obsidian_manage_tags': ['list', 'add', 'remove'],
                   'obsidian_canvas_node': ['add', 'remove'],
                   'obsidian_canvas_edge': ['add', 'remove']}.get(handler.__name__, [None])
        for action in actions:
            if action:
                kwargs['action'] = action
            result = await handler(**kwargs)
            assert secret not in result, handler.__name__
            assert quote(secret, safe='') not in result, handler.__name__


async def test_outcome_unknown_mutation_userinfo(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, 'backend', backend)
    backend.errors['refresh'] = errors.OutcomeUnknown('http://ak:sk@host')
    result = await server.obsidian_create_note('note.md', 'body')
    assert 'ak:sk' not in result and '@host' not in result
    assert 'MAY have been applied' in result
    assert 'IDEMPOTENT' in result


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_backend_info_sanitizes_reasons(tmp_path, kind):
    from tests.test_tweak_startup import vault
    async with vault(tmp_path, kind, {'tweak_values': {'PREFERRED': {'pathNormalizationVersion': 0}}}) as (backend, _):
        await backend.startup_check()
        backend._writer_unsupported_reason = 'cause Bearer Credential-Marker'
        info = await backend.backend_info()
        assert 'Credential-Marker' not in json.dumps(info)


async def test_auth_redaction_preserves_mutation_uncertainty(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, 'backend', backend)
    backend.errors['refresh'] = errors.OutcomeUnknown('Authorization:\n Credential-Marker')
    result = await server.obsidian_create_note('note.md', 'body')
    assert 'Credential-Marker' not in result
    assert 'MAY have been applied' in result
    assert 'IDEMPOTENT FOR THE SAME INPUT' in result
