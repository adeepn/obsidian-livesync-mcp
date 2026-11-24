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
