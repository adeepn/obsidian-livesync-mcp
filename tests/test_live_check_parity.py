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
        backend.sync_max_size_mb_override = 0.0001
        backend.sync_max_size_mb = 0.0001
        good, result = await live.oversize(backend, source, expected_limit=0.0001)
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
        vault = tmp_path / 'vault'
        vault.mkdir()
        assert live.local_prepare(vault, live.DECODING)
        for name, raw in live.DECODING.items():
            await backend.write_attachment('parity-live/' + name, raw, create=True,
                                           lossy_text=name == 'invalid.md')
        good, records = await live.inspect_decoding(backend, vault)
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


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_verify_rejects_wrong_or_untrusted_metadata_without_leaking(tmp_path, kind, monkeypatch, capsys):
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        vault = tmp_path / 'vault'
        vault.mkdir()
        (vault / 'a.md').write_text('public fixture\n')
        await backend.write_note('a.md', 'public fixture\n', create=True)
        original = live.raw_doc
        async def bad_size(b, path):
            return {**await original(b, path), 'size': 391}
        monkeypatch.setattr(live, 'raw_doc', bad_size)
        good, rows = await live.verify(backend, vault, ['a.md'])
        assert not good
        assert rows[0]['byte_exact'] is True
        assert rows[0]['metadata_size_matches'] is False
        async def untrusted(b, path):
            return {**await original(b, path), 'size': 'private-value', 'type': 'private-value\n'}
        monkeypatch.setattr(live, 'raw_doc', untrusted)
        good, rows = await live.verify(backend, vault, ['a.md'])
        assert not good
        assert 'private-value' not in json.dumps(rows) + capsys.readouterr().out


async def test_oversize_requires_expected_lowered_limit_before_any_access():
    class WrongLimit:
        sync_max_size_mb = 75
        async def read_note(self, path):
            pytest.fail('wrong limit must refuse before reading or publishing')
    assert not (await live.oversize(WrongLimit(), 'parity-live/exact.bin'))[0]


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_native_capture_does_not_share_stale_unprepared_content(tmp_path, kind):
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        for name in live.DECODING:
            await backend.write_note('parity-live/' + name, 'unrelated-private-material', create=True)
        good, rows = await live.inspect_decoding(backend)
        assert not good
        assert 'unrelated-private-material' not in json.dumps(rows)


@pytest.mark.parametrize('fault', ('none', 'wrong-admitted', 'missing-above', 'wrong-above'))
async def test_verify_size_checks_physical_boundaries(tmp_path, monkeypatch, fault):
    # Scale only helper fixture units to avoid large disk writes in offline tests.
    monkeypatch.setattr(live, 'MIB', 16)
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64',
                'pathNormalizationVersion': 0, 'syncMaxSizeInMB': 100}
    async with matrix_vault(tmp_path, 'couchdb', settings) as (backend, _, _, _, _):
        await backend.startup_check()
        vault = tmp_path / 'vault'
        vault.mkdir()
        folder = vault / live.PREFIX
        folder.mkdir()
        for name, count in [('device-40m.bin', 40 * live.MIB), ('exact.bin', 100 * live.MIB)]:
            data = b'x' * (1 if fault == 'wrong-admitted' else count)
            (folder / name).write_bytes(data)
            await backend.write_attachment(live.PREFIX + '/' + name, data, create=True)
        if fault != 'missing-above':
            (folder / 'above.bin').write_bytes(b'x' * (100 * live.MIB + (0 if fault == 'wrong-above' else 1)))
        monkeypatch.setattr(live, 'CouchDBBackend', lambda **kwargs: backend)
        args = live.parser().parse_args(['verify-size', '--backend', 'couchdb',
            '--state-dir', str(tmp_path / 'state'), '--vault', str(vault),
            '--output', str(tmp_path / 'result.json')])
        good, records = await live.run(args)
        assert good == (fault == 'none')


@pytest.mark.parametrize('kind', ('couchdb', 'journal'))
async def test_native_capture_withholds_unrecognized_prepared_path_content(tmp_path, kind):
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        vault = tmp_path / 'vault'
        vault.mkdir()
        assert live.local_prepare(vault, live.DECODING)
        for name in live.DECODING:
            await backend.write_note('parity-live/' + name, 'unrelated-private-material', create=True)
        good, rows = await live.inspect_decoding(backend, vault)
        assert not good
        assert 'unrelated-private-material' not in json.dumps(rows)
        assert all('chunk_data' not in row for row in rows.values())


async def test_metadata_uses_ingested_bom_size_even_if_legacy_bytes_differ(tmp_path):
    settings = {'chunkSplitterVersion': 'v2', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, 'journal', settings) as (backend, _, _, _, _):
        await backend.startup_check()
        vault = tmp_path / 'vault'
        vault.mkdir()
        raw = b'\xef\xbb\xbfabc'
        (vault / 'bom.md').write_bytes(raw)
        await backend.write_note('bom.md', raw, create=True)
        good, rows = await live.verify(backend, vault, ['bom.md'])
        assert not good and not rows[0]['byte_exact']
        assert rows[0]['metadata_size_matches'] is True
        assert rows[0]['ingested_blob_size'] == 6


@pytest.mark.parametrize('fault', ('type', 'size', 'children', 'aggregate'))
async def test_native_capture_metadata_and_aggregate_bounds(tmp_path, monkeypatch, capsys, fault):
    from content_splitter import ContentSplitter
    vault = tmp_path / 'vault'
    vault.mkdir()
    assert live.local_prepare(vault, live.DECODING)
    calls = []
    class Backend:
        _splitter = ContentSplitter({'chunkSplitterVersion': 'v3-rabin-karp'})
        async def _get_doc(self, child):
            calls.append(child)
            return {'data': 'x' * 2500}
    async def document(backend, path):
        doc = {'type': 'plain', 'size': 1, 'children': ['h:a', 'h:b']}
        if fault == 'type': doc['type'] = 'private-metadata\n'
        if fault == 'size': doc['size'] = 'private-metadata'
        if fault == 'children': doc['children'] = ['h:a'] * 257
        return doc
    monkeypatch.setattr(live, 'raw_doc', document)
    good, rows = await live.inspect_decoding(Backend(), vault)
    assert not good
    assert all('chunk_data' not in row for row in rows.values())
    assert 'private-metadata' not in json.dumps(rows) + capsys.readouterr().out
    if fault != 'aggregate':
        assert not calls
    else:
        assert all('aggregate bound' in row['capture'] for row in rows.values())
