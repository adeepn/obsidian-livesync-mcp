"""Exercise the live acceptance helper offline, including real backend doubles."""
import json
from pathlib import Path
import sys

import pytest

from scripts import live_check_parity as live
from tests.test_settings_matrix import matrix_vault


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_write_phase_and_oversize_preserve_real_backend(tmp_path, kind):
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64',
                'pathNormalizationVersion': 0, 'syncMaxSizeInMB': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        good, result = await live.write_phase(backend)
        assert good and result['moved'].endswith('moved.md')
        source = 'parity-live/exact.bin'
        await backend.write_attachment(source, b'x' * 500, create=True)
        backend.sync_max_size_mb = 0.0001
        good, result = await live.oversize(backend, source)
        assert good and result == {'limit_mib': 0.0001, 'skipped': True, 'rename_refused': True}


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_verify_includes_textual_newnotes_and_chunk_ids(tmp_path, kind):
    settings = {'chunkSplitterVersion': 'v2', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        vault = tmp_path / 'vault'
        vault.mkdir()
        for path in ('plain.md', 'text.base', 'text.json', 'text.yaml', 'upper.MD'):
            raw = 'Привет🙂\n'.encode()
            (vault / path).write_bytes(raw)
            await backend.write_note(path, raw, create=True)
        good, records = await live.verify(backend, vault, sorted(p.name for p in vault.iterdir()))
        assert good
        assert all(row['byte_exact'] and row['piece_ids_match'] for row in records)
        assert next(row for row in records if row['path'] == 'text.base')['type'] == 'newnote'
        (vault / 'text.base').write_bytes(b'changed')
        assert not (await live.verify(backend, vault, ['text.base']))[0]


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_decoding_capture_resolves_actual_backend_chunks(tmp_path, kind):
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        for name, raw in live.DECODING.items():
            await backend.write_attachment('parity-live/' + name, raw, create=True,
                                           lossy_text=name == 'invalid.md')
        good, records = await live.inspect_decoding(backend)
        assert good
        assert records['parity-live/invalid.md']['type'] == 'plain'
        assert records['parity-live/text.png']['type'] == 'newnote'
        assert records['parity-live/upper.MD']['type'] == 'newnote'
        assert all(row['chunk_data'] and row['children'] for row in records.values())


def test_milestone_allowlist_never_leaks_secret_or_unknown_values():
    secret = 'must-never-appear-in-artifact'
    result = live.sanitized_milestone({'tweak_values': {'private-device-name': {
        'hashAlg': 'sha1', 'minimumChunkSize': 5, 'idDerivationKey': secret,
        'pbkdf2salt': secret, 'passphrase': secret, 'newUnknownSetting': secret,
        'syncOnlyRegEx': secret, 'tweakModified': float('inf')}}})
    encoded = json.dumps(result)
    assert secret not in encoded and 'private-device-name' not in encoded
    assert result['tweak_values']['device-1']['values']['hashAlg'] == 'sha1'
    assert 'newUnknownSetting' in result['unknown_keys']
    assert result['tweak_values']['device-1']['values']['tweakModified'] == '<withheld>'


def test_local_prepare_is_exclusive_and_preserves_existing_bytes(tmp_path):
    assert live.local_prepare(tmp_path, {'a.bin': 5, 'b.md': b'hello'})
    assert (tmp_path / 'parity-live/a.bin').stat().st_size == 5
    assert not live.local_prepare(tmp_path, {'a.bin': b'replace'})
    assert (tmp_path / 'parity-live/a.bin').stat().st_size == 5


def test_failure_exit_and_json_do_not_include_exception_text(tmp_path, monkeypatch, capsys):
    async def fail(args):
        raise RuntimeError('credential-material-not-for-output')
    monkeypatch.setattr(live, 'run', fail)
    output = tmp_path / 'result.json'
    monkeypatch.setattr(sys, 'argv', ['live_check_parity.py', 'reconnect',
                        '--state-dir', str(tmp_path / 'state'), '--vault', str(tmp_path),
                        '--output', str(output)])
    assert live.main() == 1
    assert 'credential-material' not in capsys.readouterr().out + output.read_text()
    assert json.loads(output.read_text())['passed'] is False
