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
    assert 'newUnknownSetting' not in encoded
    assert result['unknown_keys']['count'] > 0
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


def test_unknown_milestone_key_names_are_digest_only():
    # P-19: remote strings are not public fields, including property names.
    import hashlib
    private = 'FAKE_PRIVATE_TOKEN_0123456789'
    result = live.sanitized_milestone({'tweak_values': {'PREFERRED': {private: 7}}})
    assert private not in json.dumps(result)
    assert result['unknown_keys'] == {
        'marker': '<unknown keys withheld>', 'count': 1,
        'sha256': hashlib.sha256(json.dumps([private], ensure_ascii=True,
                                           separators=(',', ':')).encode()).hexdigest()}


@pytest.mark.parametrize('command', ['write-large', 'write-phase', 'oversize'])
async def test_mutations_require_flags_and_exact_target_before_backend(tmp_path, monkeypatch, command):
    def no_backend(**kwargs):
        pytest.fail('must validate authorization before constructing backend')
    monkeypatch.setattr(live, 'CouchDBBackend', no_backend)
    args = live.parser().parse_args([command, '--backend', 'couchdb',
        '--state-dir', str(tmp_path / 'state'), '--vault', str(tmp_path),
        '--output', str(tmp_path / 'result.json')])
    assert not (await live.run(args))[0]
    args.allow_remote_write = True
    args.confirm_target = 'wrong-database'
    assert not (await live.run(args))[0]
    args.confirm_target = args.database
    if command != 'write-large':
        assert not (await live.run(args))[0]


def test_selection_arguments_default_to_vault_policy(tmp_path):
    args = live.parser().parse_args(['reconnect', '--state-dir', str(tmp_path),
        '--vault', str(tmp_path), '--output', str(tmp_path / 'report.json')])
    assert args.use_ignore_files is None and args.ignore_files is None


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_reconnect_classifies_ignored_and_oversized_with_production_policy(tmp_path, kind):
    # C:targetFilter.ts:132-146 and services/base/VaultService.ts:73-81.
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64',
                'pathNormalizationVersion': 0, 'syncMaxSizeInMB': 0.0001,
                'useIgnoreFiles': True, 'ignoreFiles': '.gitignore'}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        from tests.test_selection_settings import seed_hidden_ignore
        await seed_hidden_ignore(backend, 'ignored.md\n')
        await backend.write_note('expected.md', 'expected', create=True)
        vault = tmp_path / 'disk'
        vault.mkdir()
        for name, raw in [('ignored.md', b'x'), ('large.bin', b'x' * 200),
                          ('expected.md', b'expected'), ('missing.md', b'missing')]:
            (vault / name).write_bytes(raw)
        good, result = await live.reconnect_files(backend, vault)
        assert not good
        assert result['expected'] == 2
        assert result['excluded_by_policy'] == {'ignoreFiles': 1, 'syncMaxSizeInMB': 1}
        assert result['missing'] == 1
        assert result['size_or_hash_mismatch'] == 0
        assert len(result['files']) == 2


def test_ignore_cli_uses_production_js_trim(tmp_path):
    # C:serviceFeatures/targetFilter.ts:95-98 uses JS trim, not str.strip.
    from livesync_format import split_ignore_names
    assert live.ignore_names('\ufeffrules.txt\ufeff,') == split_ignore_names('\ufeffrules.txt\ufeff,')
    assert live.ignore_names('') is None


@pytest.mark.parametrize('backend', ['s3', 'couchdb'])
@pytest.mark.parametrize('command', ['write-large', 'write-phase', 'oversize'])
def test_exact_mutation_gates_accept_only_complete_authorization(tmp_path, backend, command):
    args = live.parser().parse_args([command, '--backend', backend,
        '--state-dir', str(tmp_path), '--vault', str(tmp_path),
        '--output', str(tmp_path / 'report.json'), '--allow-remote-write',
        '--allow-remote-delete'])
    args.confirm_target = args.bucket + '/' + args.prefix if backend == 's3' else args.database
    assert live.mutation_authorized(args)
    args.confirm_target += 'wrong'
    assert not live.mutation_authorized(args)
    args.confirm_target = args.bucket + '/' + args.prefix if backend == 's3' else args.database
    args.allow_remote_write = False
    assert not live.mutation_authorized(args)
    args.allow_remote_write = True
    args.allow_remote_delete = False
    assert live.mutation_authorized(args) == (command == 'write-large')


async def test_default_reconnect_never_publishes_or_leaks_unknown_key(tmp_path, monkeypatch, capsys):
    private = 'FAKE_PRIVATE_TOKEN_0123456789'
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64',
                'pathNormalizationVersion': 0, private: True}
    async with matrix_vault(tmp_path, 'journal', settings) as (backend, _, _, calls, storage):
        await backend.startup_check()
        async def forbidden(*args, **kwargs):
            pytest.fail('read-only reconnect must not publish, rename or delete')
        for name in ('write_note', 'write_attachment', 'rename_note', 'delete_note'):
            monkeypatch.setattr(backend, name, forbidden)
        monkeypatch.setattr(live, 'Boto3ObjectStorage', lambda cfg: storage)
        vault = tmp_path / 'disk'; vault.mkdir()
        (vault / 'missing.md').write_text('missing')
        args = live.parser().parse_args(['reconnect', '--vault', str(vault),
            '--state-dir', str(tmp_path / 'state'), '--output', str(tmp_path / 'report.json')])
        good, result = await live.run(args)
        assert not good and result['milestone']['unknown_keys']['count'] == 1
        assert private not in json.dumps(result) + capsys.readouterr().out
        assert not calls


def test_helper_has_no_wipe_reset_or_physical_deletion_calls():
    import ast
    tree = ast.parse(Path(live.__file__).read_text())
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {'delete', 'delete_object', 'delete_objects', 'remove', 'unlink',
                        'rmtree', 'reset', 'wipe', 'purge', 'destroy'}


@pytest.mark.parametrize('kind', ['missing', 'file'])
async def test_reconnect_refuses_invalid_vault_directory(tmp_path, kind):
    vault = tmp_path / 'not-a-vault'
    if kind == 'file':
        vault.write_text('ordinary file')
    good, result = await live.reconnect_files(object(), vault)
    assert not good
    assert result == {'cause': 'vault_directory_missing'}


@pytest.mark.parametrize('kind', ['couchdb', 'journal'])
async def test_reconnect_reports_existing_mismatch_separately_from_missing(tmp_path, kind):
    settings = {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64',
                'pathNormalizationVersion': 0}
    async with matrix_vault(tmp_path, kind, settings) as (backend, _, _, _, _):
        await backend.startup_check()
        await backend.write_note('changed.md', 'remote', create=True)
        vault = tmp_path / 'disk'; vault.mkdir()
        (vault / 'changed.md').write_text('local')
        (vault / 'missing.md').write_text('missing')
        good, result = await live.reconnect_files(backend, vault)
        assert not good and result['missing'] == 1
        assert result['size_or_hash_mismatch'] == 1


@pytest.mark.parametrize('kind', ['journal', 'couchdb'])
async def test_present_note_is_not_missing_when_type_metadata_corrupt(tmp_path, monkeypatch, kind):
    async with matrix_vault(tmp_path, kind, {'chunkSplitterVersion': 'v3-rabin-karp', 'hashAlg': 'xxhash64', 'pathNormalizationVersion': 0}) as (backend, *_):
        await backend.startup_check()
        await backend.write_note('exists.md', 'public', create=True)
        vault = tmp_path / 'disk'
        vault.mkdir()
        (vault / 'exists.md').write_text('public')
        original = live.raw_doc
        async def corrupt(b, path):
            return {**await original(b, path), 'type': 'untrusted-invalid-type'}
        monkeypatch.setattr(live, 'raw_doc', corrupt)
        good, report = await live.reconnect_files(backend, vault)
        assert not good
        assert report['files'][0]['byte_exact']
        assert report['missing'] == 0


def test_owner_python_commands_compile_and_growth_is_valid_text(tmp_path):
    """The size-policy recipe must not depend on the native invalid-byte experiment."""
    import re
    import shlex
    import subprocess
    commands = re.findall(r'`(\./venv/bin/python -c [^`]+)`',
                          (Path(live.__file__).parents[1] / 'docs/live-check-parity-checklist.md').read_text())
    assert commands
    growth = []
    for command in commands:
        args = shlex.split(command)
        compile(args[2], '<documented owner command>', 'exec')
        if 'local_prepare' in args[2] and 'growth.md' in args[2]:
            growth.append(args[2])
    assert len(growth) == 1
    vault = tmp_path / 'vault'; vault.mkdir()
    result = subprocess.run([sys.executable, '-c', growth[0], str(vault)],
                            capture_output=True, text=True, cwd=Path(live.__file__).parents[1])
    assert result.returncode == 0, result.stderr
    raw = (vault / 'parity-live/growth.md').read_bytes()
    assert len(raw) == 1048576
    assert raw.decode('utf-8') == 'x' * 1048576
