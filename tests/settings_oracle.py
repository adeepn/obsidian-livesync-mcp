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
# Every emitted native vector must include the independent lossless view.
for _vector in ([v for row in _oracle['matrix'] for v in row['payloads'].values()]
                + _oracle['grid']):
    if not {'lossless_pieces', 'lossless_children', 'lossless_split_pieces',
            'lossless_split_children'} <= _vector.keys():
        raise AssertionError('Stale lossless settings oracle; regenerate with node scripts/extract_settings_oracle.mjs')
del _vector
ORACLE = _oracle
del _oracle
MATRIX = {v['id']: v for v in ORACLE['matrix']}
GRID = {v['id']: v for v in ORACLE['grid']}


def matrix_vector(case):
    vector = MATRIX[case_id(case)]  # Missing oracle rows fail, never fall back.
    assert vector['settings'] == settings_for(case)
    return vector


def expected_pieces(vector):
    return [ORACLE['pieces'][i] for i in vector.get('lossless_pieces', vector['pieces'])]


def assert_partition(pieces, children, vector):
    assert pieces == expected_pieces(vector), 'PARTITION pieces mismatch'
    assert children == vector.get('lossless_children', vector['children']), 'PARTITION children mismatch'
