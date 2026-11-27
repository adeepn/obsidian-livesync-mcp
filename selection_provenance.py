"""Verify installed selection bytes before launching any native runtime."""
import hashlib
import json
from pathlib import Path


class SelectionProvenanceError(ValueError):
    pass


def relative_name(root: Path, path: Path) -> str:
    """Manifest names are POSIX paths on every platform; the verifier joins with '/'."""
    return path.relative_to(root).as_posix()


def verify_selection_files(root: Path):
    try:
        base = root / 'vendor/selection'
        manifest = base / 'installed-sha256.json'
        value = json.loads(manifest.read_text())
        if type(value) is not dict or set(value) != {'version', 'files'} or value['version'] != 1:
            raise ValueError
        files = value['files']
        expected = {relative_name(root, p) for p in base.rglob('*') if p.is_file() and p != manifest}
        expected |= {'selection_worker.mjs', 'scripts/selection_provenance.mjs'}
        if type(files) is not dict or set(files) != expected:
            raise ValueError
        for name, digest in files.items():
            path = root / name
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError
            if type(digest) is not str or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError
    except (OSError, ValueError, TypeError):
        raise SelectionProvenanceError('selection provenance mismatch; regenerate the verified installation') from None
