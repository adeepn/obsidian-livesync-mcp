"""Load the committed oracle once; tests never execute Node or production code."""
import gzip
import hashlib
import json
from pathlib import Path

from tests.settings_oracle_inputs import case_id, settings_for

INPUTS = Path(__file__).with_name('settings_oracle_inputs.py')
_oracle = json.loads(gzip.decompress(
    (Path(__file__).parent / 'fixtures/settings_oracle_upstream.json.gz').read_bytes()))
if hashlib.sha256(INPUTS.read_bytes()).hexdigest() != _oracle['inputs_sha256']:
    raise AssertionError(
        'Stale settings oracle; regenerate with node scripts/extract_settings_oracle.mjs '
        'COMMONLIB WASM_ESM WHEELS_DIR WHEELS_TARBALL OUTPUT.json.gz WASM_TARBALL')
ORACLE = _oracle
del _oracle
MATRIX = {v['id']: v for v in ORACLE['matrix']}
GRID = {v['id']: v for v in ORACLE['grid']}


def matrix_vector(case):
    vector = MATRIX[case_id(case)]  # Missing oracle rows fail, never fall back.
    assert vector['settings'] == settings_for(case)
    return vector


def expected_pieces(vector):
    return [ORACLE['pieces'][i] for i in vector['pieces']]


def assert_partition(pieces, children, vector):
    assert pieces == expected_pieces(vector), 'PARTITION pieces mismatch'
    assert children == vector['children'], 'PARTITION children mismatch'
