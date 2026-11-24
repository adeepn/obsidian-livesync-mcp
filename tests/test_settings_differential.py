"""Exhaustive cheap settings interactions against executable pinned upstream."""
import pytest

from content_splitter import ContentSplitter
from tests.settings_oracle import GRID, MATRIX, assert_partition, check_input_digest
from tests.settings_oracle_inputs import (
    CASES, BRANCH_CASES, SWEEP_CASES, case_id, grid_cases, grid_payload,
    grid_path, settings_for,
)


def test_oracle_covers_exact_inputs():
    check_input_digest()
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
    pieces = splitter.split(data, path=grid_path(kind))
    children = [splitter.chunk_id_for_piece(p) for p in pieces]
    assert_partition(pieces, children, {
        'pieces': vector['split_pieces'], 'children': vector['split_children']})


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
        'pieces': vector['split_pieces'], 'children': vector['split_children']})
