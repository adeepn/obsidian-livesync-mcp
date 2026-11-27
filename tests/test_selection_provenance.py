"""Installed-byte provenance is peer policy, before native path.ts:241-265 runs."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import selection_engine


@pytest.fixture
def installation(tmp_path):
    shutil.copytree('vendor/selection', tmp_path / 'vendor/selection')
    shutil.copy('selection_worker.mjs', tmp_path)
    (tmp_path / 'scripts').mkdir()
    helper = Path('scripts/selection_provenance.mjs')
    if helper.exists():
        shutil.copy(helper, tmp_path / 'scripts')
    return tmp_path


@pytest.mark.parametrize('target', ['vendor/selection/upstream.mjs',
    'vendor/selection/node_modules/minimatch/dist/esm/index.js', 'selection_worker.mjs'])
@pytest.mark.parametrize('damage', ['tamper', 'missing'])
def test_worker_refuses_before_any_node_launch(installation, monkeypatch, target, damage):
    path = installation / target
    if damage == 'missing':
        path.unlink()
    else:
        path.write_text("throw Error('PRIVATE_TAMPER');\n" + path.read_text())
    monkeypatch.setattr(selection_engine, '__file__', str(installation / 'selection_engine.py'))
    launches = []
    def forbidden(*args, **kwargs):
        launches.append(args)
        raise AssertionError('Node must not start')
    monkeypatch.setattr(selection_engine.subprocess, 'run', forbidden)
    monkeypatch.setattr(selection_engine.subprocess, 'Popen', forbidden)
    with pytest.raises(selection_engine.EngineFailure) as caught:
        selection_engine.SelectionWorker()._start()
    assert caught.value.cause == 'provenance-mismatch'
    assert 'PRIVATE_TAMPER' not in str(caught.value)
    assert 'regenerate' in str(caught.value)
    assert launches == []


@pytest.mark.parametrize('damage', ['tamper', 'missing'])
def test_direct_worker_checks_before_import(installation, damage):
    marker = installation / 'executed'
    module = installation / 'vendor/selection/upstream.mjs'
    if damage == 'missing':
        module.unlink()
    else:
        module.write_text("import fs from 'node:fs'; fs.writeFileSync(" + repr(str(marker)) + ", 'executed');\n" + module.read_text())
    result = subprocess.run(['node', str(installation / 'selection_worker.mjs')],
        input='{"id":1,"op":"selftest"}\n', text=True, capture_output=True, timeout=60)
    assert result.returncode != 0
    assert not marker.exists()
    assert 'selection provenance mismatch' in result.stderr


def test_hashed_files_keep_their_bytes_on_checkout():
    # A checkout with core.autocrlf=true must not change the bytes the manifest hashes.
    names = sorted(json.loads(Path('vendor/selection/installed-sha256.json').read_text())['files'])
    attributes = subprocess.run(['git', 'check-attr', 'text', '--', *names],
                                capture_output=True, text=True, check=True).stdout
    assert all(line.endswith(': text: unset') for line in attributes.splitlines())


def test_manifest_names_are_posix_paths_on_every_platform():
    from pathlib import PurePosixPath, PureWindowsPath
    from selection_provenance import relative_name
    windows = PureWindowsPath('C:/peer')
    assert relative_name(windows, windows / 'vendor/selection/node_modules/minimatch/dist/esm/index.js') == (
        'vendor/selection/node_modules/minimatch/dist/esm/index.js')
    assert relative_name(PurePosixPath('/peer'), PurePosixPath('/peer/selection_worker.mjs')) == 'selection_worker.mjs'
    assert relative_name(Path('.'), Path('vendor/selection/upstream.mjs')) == 'vendor/selection/upstream.mjs'


def test_installed_tree_is_verified():
    from selection_provenance import verify_selection_files
    verify_selection_files(Path.cwd())


@pytest.mark.parametrize('generator', ['extract_selection_vectors.mjs', 'extract_ignore_name_vectors.mjs'])
def test_oracle_verifies_before_vendor_import(installation, generator):
    shutil.copy(Path('scripts') / generator, installation / 'scripts')
    module = installation / 'vendor/selection/node_modules/minimatch/dist/esm/index.js'
    marker = installation / 'oracle-executed'
    module.write_text("import fs from 'node:fs'; fs.writeFileSync(" + repr(str(marker)) + ", 'executed');\n" + module.read_text())
    result = subprocess.run(['node', str(installation / 'scripts' / generator)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert not marker.exists()
    assert 'selection provenance mismatch' in result.stderr
