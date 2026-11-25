"""Integration regressions; the matcher and semantic fixtures stay untouched."""
import json
import subprocess
from types import SimpleNamespace

import pytest
import livesync_format as fmt
from tests.test_target_filter import backend
from storage_base import StorageError


@pytest.mark.parametrize('payload', [
    {'allowed': 'false'},
    {'allowed': 'false', 'rule': None, 'writer_supported': True, 'reason': None},
    {'allowed': True, 'rule': 1, 'writer_supported': True, 'reason': None},
    {'allowed': True, 'rule': None, 'writer_supported': 1, 'reason': None},
    {'allowed': True, 'rule': None, 'writer_supported': True, 'reason': []},
])
async def test_allowed_string_false_denies_write(backend, monkeypatch, payload):
    b, mutations = backend
    b.selection_only = 'md'
    from selection_engine import SelectionWorker
    original = SelectionWorker._exchange
    def response(self, op, data=None):
        return payload if op == 'evaluate' else original(self, op, data)
    monkeypatch.setattr(SelectionWorker, '_exchange', response)
    before = mutations()
    with pytest.raises(StorageError, match='malformed-output'):
        await b.write_note('safe.md', 'denied', create=True)
    assert mutations() == before


@pytest.mark.parametrize('cause', ['node-missing', 'node-version-too-old'])
async def test_startup_requires_capable_engine(backend, monkeypatch, cause):
    b, _ = backend
    b.selection_only = 'md'
    def run(*a, **k):
        if cause == 'node-missing':
            raise FileNotFoundError('private payload')
        return SimpleNamespace(stdout='v20.1.0', returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(StorageError, match=cause):
        await b.startup_check()
    info = await b.backend_info()
    assert info['selection_engine_state'] == 'unsupported'


async def test_off_never_launches_node(backend, monkeypatch):
    b, _ = backend
    def forbidden(*a, **k):
        pytest.fail('Node launched with filters off')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    await b.startup_check()
    await b.write_note('plain.md', 'yes', create=True)
    assert (await b.backend_info())['selection_engine_state'] == 'off'


@pytest.mark.parametrize('cause', ['timeout', 'nonzero-exit (status 17)', 'malformed-output'])
async def test_engine_failure_denies_without_mutation(backend, monkeypatch, cause):
    b, mutations = backend
    b.selection_only = 'md'
    def run(*a, **k):
        if cause == 'timeout':
            raise subprocess.TimeoutExpired('private payload', 5)
        return SimpleNamespace(stdout='private payload', returncode=17 if cause.startswith('nonzero') else 0)
    monkeypatch.setattr(subprocess, 'run', run)
    before = mutations()
    with pytest.raises(StorageError) as error:
        await b.write_note('safe.md', 'denied', create=True)
    assert cause in str(error.value)
    assert 'private payload' not in str(error.value)
    assert mutations() == before


def test_capability_probe_has_scheduler_margin(monkeypatch):
    """Integration startup is not a five-second scheduling benchmark.

    Native targetFilter.ts:132-149 has no subprocess deadline. Production
    configurable deadline behavior remains covered by test_transport_limits.
    """
    from selection_engine import capability_version_check
    def run(args, **kwargs):
        assert args == ['node', '--version']
        assert kwargs['timeout'] >= 60
        return SimpleNamespace(stdout='v26.13.1', returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    capability_version_check()
