"""P-17: preserve actionable device refusals at the MCP boundary."""
import pytest
import server
import storage_base as errors
from tests.test_upload_attachment import backend


@pytest.mark.parametrize('rule', ['syncOnlyRegEx', 'syncIgnoreRegEx', 'ignoreFiles'])
async def test_selection_refusal_names_rule_and_path(backend, monkeypatch, rule):
    from livesync_format import TargetVerdict
    async def verdict(path):
        return TargetVerdict(allowed=False, rule=rule)
    monkeypatch.setattr(backend, 'selection_for', verdict)
    monkeypatch.setattr(server, 'backend', backend)
    result = await server.obsidian_create_note('excluded.md', 'body')
    assert "path 'excluded.md' is excluded" in result
    assert rule in result
    assert 'devices would not synchronize it' in result


@pytest.mark.parametrize('kind', [errors.WriterUnsupported, errors.MutationNotApplied])
async def test_specific_mutation_refusals_reach_tool(backend, monkeypatch, kind):
    monkeypatch.setattr(server, 'backend', backend)
    def refuse():
        raise kind('hashAlg unsupported; https://user:secret@internal/db _rev=private token=hidden')
    monkeypatch.setattr(backend, 'check_writer_support', refuse)
    result = await server.obsidian_create_note('affected.md', 'body')
    assert 'affected.md' in result
    assert 'hashAlg unsupported' in result
    if kind is errors.MutationNotApplied:
        assert 'note is unchanged' in result
    for private in ('secret', 'internal', 'private', 'hidden'):
        assert private not in result


def descendants(cls):
    return [cls] + [child for sub in cls.__subclasses__() for child in descendants(sub)]


@pytest.mark.parametrize('kind', descendants(errors.StorageError))
def test_every_storage_error_has_a_specific_safe_message(kind):
    # Constructors model structured outcomes; bypass only to exercise dispatch
    # uniformly for every loaded subclass, including future subclasses.
    error = kind.__new__(kind)
    Exception.__init__(error, 'specific safe diagnostic')
    error.__dict__.update(path='note.md', size=1, maximum=2, source='a.md',
                          destination='b.md', source_state='live', detail='diagnostic')
    result = server._storage_message(error)
    assert 'unable to access vault storage' not in result


@pytest.mark.parametrize("parent", [errors.StorageError, errors.StorageUnavailable, errors.ConcurrentModification])
def test_unmapped_error_preserves_safe_detail_and_redacts(parent):
    class FutureError(parent):
        pass
    result = server._storage_message(FutureError('safe cause https://user:secret@internal/db _rev=private password=hidden'))
    assert 'safe cause' in result
    for private in ('secret', 'internal', 'private', 'hidden'):
        assert private not in result


@pytest.mark.parametrize('diagnostic,private', [
    ('safe cause Authorization: Bearer credential-marker', 'credential-marker'),
    ('safe cause {"password": "credential-marker"}', 'credential-marker'),
    ("safe cause {'_rev': 'revision-marker'}", 'revision-marker'),
    ('safe cause idDerivationKey=key-marker', 'key-marker'),
    ('safe cause access_key=access-marker secret_key=secret-marker', 'secret-marker'),
])
def test_structured_storage_diagnostics_redact_credentials(diagnostic, private):
    result = server._storage_message(errors.StorageError(diagnostic))
    assert 'safe cause' in result
    assert private not in result
