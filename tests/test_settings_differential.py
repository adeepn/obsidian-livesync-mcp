"""Exhaustive cheap settings interactions against executable pinned upstream."""
import pytest

from content_splitter import ContentSplitter
from tests.settings_oracle import GRID, MATRIX, assert_partition
from tests.settings_oracle_inputs import (
    CASES, BRANCH_CASES, SWEEP_CASES, case_id, grid_cases, grid_payload,
    grid_path, settings_for, is_text_blob,
)


def test_oracle_covers_exact_inputs():
    assert set(MATRIX) == {case_id(c) for c in CASES + BRANCH_CASES}
    assert set(GRID) == {name for name, _, _ in grid_cases()}


@pytest.mark.parametrize('name,settings,kind', list(grid_cases()),
                         ids=[name for name, _, _ in grid_cases()])
def test_settings_splitter_differential(name, settings, kind, monkeypatch):
    def no_subprocess(*args, **kwargs):
        pytest.fail("offline differential test must never launch a subprocess")
    monkeypatch.setattr("subprocess.Popen", no_subprocess)
    vector = GRID[name]
    assert vector['settings'] == settings
    assert vector['payload'] == kind
    suffix, data = grid_payload(kind)
    splitter = ContentSplitter(settings)
    pieces = splitter.split(data, path=grid_path(kind), text_blob=is_text_blob(kind))
    children = [splitter.chunk_id_for_piece(p) for p in pieces]
    assert_partition(pieces, children, {
        'pieces': vector['lossless_split_pieces'], 'children': vector['lossless_split_children']})


@pytest.mark.parametrize('case', SWEEP_CASES, ids=case_id)
def test_v3_minimum_boundary_sweep(case):
    """Hold a seeded payload fixed while sweeping the grouping threshold."""
    settings = settings_for(case)
    vector = GRID[case_id(case)]
    assert vector['settings'] == settings
    kind = case[-1]
    splitter = ContentSplitter(settings)
    pieces = splitter.split(grid_payload(kind)[1], path=grid_path(kind))
    assert_partition(pieces, [splitter.chunk_id_for_piece(p) for p in pieces], {
        'pieces': vector['lossless_split_pieces'], 'children': vector['lossless_split_children']})


def test_upstream_scalar_minimum_boundaries():
    """Exact fractional loop equalities cannot be lengths of real Blobs.

    Oracle runs the binary preamble verbatim from pinned chunks.ts. Real
    integer-length probes remain in the full partition differential above.
    """
    from content_splitter import _binary_minimum
    from tests.settings_oracle import ORACLE
    for probe in ORACLE['scalar_probes']:
        assert _binary_minimum(probe['size'], probe['path']) == probe['minimum'], probe


def test_equality_catalogue_is_complete():
    from tests.settings_oracle_inputs import EQUALITY_CASES, EQUALITY_OPERATORS
    assert len(EQUALITY_OPERATORS) == 20
    names = {case_id(c) for c in EQUALITY_CASES}
    assert names <= set(GRID)
    for label, cases in EQUALITY_OPERATORS.items():
        assert cases, label
        assert set(cases) <= names
        for seed in ((198, 262, 861) if label == 'V3 candidate minimum equality' else (0, 1, 2)):
            assert any(f':{seed}:' in name for name in cases), label
