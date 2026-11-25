"""One measured oracle witness or cited proof for every comparison site."""
import json
from pathlib import Path

import pytest
import content_splitter

from tests.settings_oracle import ORACLE, GRID, expected_pieces
from tests.splitter_observability import (
    SOURCE_PATH, comparison_sites, isolated_mutant, grid_result, helper_result,
    equality_mutation, MutationFuelExhausted, coercion_result,
)

INVENTORY = json.loads(Path(__file__).with_name('splitter_comparisons.json').read_text())


def test_comparison_inventory_covers_source():
    assert {(e['line'], e['column'], e['function'], e['source'], e['operator']) for e in INVENTORY} == set(
        comparison_sites(SOURCE_PATH.read_text()))
    assert len(INVENTORY) == len({(e['line'], e['column'], e['operator']) for e in INVENTORY})
    for entry in INVENTORY:
        assert entry['mutation'] == equality_mutation(entry['source'], entry['operator'])
        assert entry['citation']
        assert ('witness' in entry) != ('proof' in entry)
        if 'proof' in entry:
            assert entry['class'] in ('a', 'b', 'c', 'G3-owned')
            assert entry['proof'].strip()
            if entry['class'] == 'G3-owned':
                assert entry['line'] == 294 and entry['source'] == 'value >= 0'


@pytest.mark.parametrize('entry', [e for e in INVENTORY if 'witness' in e],
                         ids=lambda e: f"{e['function']}:{e['line']}:{e['column']}:{e['operator']}")
def test_comparison_equality_is_observable(entry):
    mutant = isolated_mutant(entry)
    witness = entry['witness']
    if witness['kind'] == 'grid':
        vector = GRID[witness['id']]
        expected = expected_pieces({'pieces': vector['split_pieces']}), vector['split_children']
        run = lambda module: grid_result(module, vector)
    elif witness['kind'] == 'cell':
        from tests.test_legacy_cells import ALL_CELL_ROWS, cell_run
        row = next(r for r in ALL_CELL_ROWS if r['id'] == witness['id'])
        assert row['status'] in ('time_limit', 'step_limit')
        expected = ('refused',)
        def run(module):
            try:
                return cell_run(module, row)
            except ValueError:
                return ('refused',)
    elif witness['kind'] == 'coercion':
        from tests.test_coercion_cells import ROWS, refuses
        row = ROWS[witness['index']]
        expected = ('refused',) if refuses(row) else (row['split_pieces'], row['split_children'])
        run = lambda module: coercion_result(module, row)
    elif witness['kind'] == 'helper':
        vector = ORACLE['helper_probes'][witness['family']][witness['index']]
        expected = (vector['base36'], vector['raw_hash']) if 'value' in vector else vector['base64']
        run = lambda module: helper_result(module, vector)
    else:
        fixture = json.loads(Path('tests/fixtures/splitter_migration_upstream.json').read_text())
        vector = fixture['vectors'][witness['index']]
        expected = vector['expected']
        run = lambda module: module.normalize_splitter_settings(vector['input'])
    assert run(content_splitter) == expected, entry
    try:
        result = run(mutant)
    except (IndexError, ValueError, TypeError, ZeroDivisionError, RecursionError, MutationFuelExhausted):
        # The mutant differs from the upstream result or the bounded refusal.
        return
    assert result != expected, entry
