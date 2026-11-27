"""Fetch verified ES modules from the exact upstream lock; never run npm scripts.

Usage: python scripts/vendor_selection.py /path/to/pinned/commonlib
"""
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection_provenance import relative_name

PIN = '484c164450d61deb2e301582b60cb9b5f9fa977f'
upstream = Path(sys.argv[1])
pinned = subprocess.check_output(
    ['git', '-C', str(upstream), 'show', f'{PIN}:package-lock.json']
)
if pinned != (upstream / 'package-lock.json').read_bytes():
    raise ValueError('Modified upstream lockfile')
lock = json.loads(pinned)
root = Path('vendor/selection/node_modules')
manifest = {}
for name in ('minimatch', 'brace-expansion', 'balanced-match',
             '@isaacs/nonrecursive-glob-match'):
    package = lock['packages']['node_modules/' + name]
    if not package['resolved'].startswith('https://registry.npmjs.org/'):
        raise ValueError('Unexpected dependency registry')
    data = urllib.request.urlopen(package['resolved'], timeout=30).read()
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if integrity != package['integrity']:
        raise ValueError('Archive integrity mismatch: ' + name)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for member in archive.getmembers():
            rel = PurePosixPath(member.name).relative_to('package')
            if '..' in rel.parts or rel.is_absolute():
                raise ValueError('Unsafe archive path')
            retained = (str(rel).startswith('dist/esm/')
                        or str(rel) in ('package.json', 'LICENSE', 'LICENSE.md'))
            if not member.isfile() or not retained or str(rel).endswith(('.map', '.ts')):
                continue
            output = root / name / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.extractfile(member).read())
    manifest[name] = {
        'version': package['version'], 'url': package['resolved'],
        'sha256': hashlib.sha256(data).hexdigest(), 'integrity': integrity,
    }
Path('vendor/selection/sources.json').write_text(json.dumps(manifest, indent=2) + '\n')

# Manifest covers every installed byte, not just acquisition archives.
base = Path('vendor/selection')
files = {relative_name(Path('.'), p): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(base.rglob('*')) if p.is_file() and p.name != 'installed-sha256.json'}
for name in ('selection_worker.mjs', 'scripts/selection_provenance.mjs'):
    files[name] = hashlib.sha256(Path(name).read_bytes()).hexdigest()
(base / 'installed-sha256.json').write_text(json.dumps({'version': 1, 'files': dict(sorted(files.items()))}, indent=2) + '\n')
