"""Every consumer must reject stale recipes before fixtures are exported."""
from pathlib import Path

import pytest


def test_stale_input_is_rejected_before_export(monkeypatch):
    source = Path('tests/settings_oracle.py')
    inputs = Path('tests/settings_oracle_inputs.py').resolve()
    read_bytes = Path.read_bytes

    def stale_bytes(path):
        data = read_bytes(path)
        return data + b'\n# stale recipe\n' if path.resolve() == inputs else data

    monkeypatch.setattr(Path, 'read_bytes', stale_bytes)
    namespace = {'__file__': str(source.resolve())}
    with pytest.raises(AssertionError, match='node scripts/extract_settings_oracle.mjs'):
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
    assert not {'ORACLE', 'GRID', 'MATRIX'} & namespace.keys()


def test_acceptance_oracle_provenance():
    # Native ContentSplitterV2.ts:20-32 and chunks.ts:342-433; published
    # wheels and WASM integrity must match the existing pinned oracle.
    import hashlib
    import json
    from tests.settings_oracle import ORACLE
    fixture = json.loads(Path('tests/fixtures/acceptance_upstream.json').read_text())
    for field, name in [('generator_sha256', 'scripts/extract_acceptance_vectors.mjs'),
                        ('harness_sha256', 'scripts/extract_settings_oracle.mjs')]:
        assert fixture[field] == hashlib.sha256(Path(name).read_bytes()).hexdigest()
    assert {v['size_bytes'] for v in fixture['rows']} == {40 * 1048576, 100 * 1048576}
    for row in fixture['rows']:
        assert row['upstream_commit'] == ORACLE['upstream_commit']
        assert row['decoder_dependency'] == ORACLE['decoder_dependency']
        assert row['wasm_dependency'] == ORACLE['wasm_dependency']
        assert all(ORACLE['source_sha256'][name] == digest for name, digest in row['source_sha256'].items())
        assert len(row['pieces']) == len(row['children'])
        assert sum(p['bytes'] for p in row['pieces']) == row['size_bytes']
