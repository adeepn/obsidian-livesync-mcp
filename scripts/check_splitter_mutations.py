"""Replay known splitter mutations and G3 edge-parameter flips in memory.

Run with ./venv/bin/python scripts/check_splitter_mutations.py OUTPUT_DIR.
Each isolated process runs the committed exact grid/CouchDB/journal witness
nodes, without fail-fast or skipping an already caught bucket. The manifest
contains mutation definitions and input selectors, never expected outputs.
"""
import ast
import json
from pathlib import Path
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MANIFEST = ROOT / 'tests/splitter_mutations.json'


def mutated_source(definition):
    module_name = definition.get('module', 'content_splitter')
    assert module_name in ('content_splitter', 'livesync_format')
    source = (ROOT / (module_name + '.py')).read_text()
    tree = ast.parse(source)
    owner = tree if definition['owner'] == 'module' else next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == definition['owner'])
    function = next(n for n in owner.body if isinstance(n, ast.FunctionDef)
                    and n.name == definition['function'])
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[:function.lineno - 1]))
    end = sum(map(len, lines[:function.end_lineno]))
    indent = ' ' * function.col_offset
    body = textwrap.dedent(source[start:end])
    if 'line' in definition:
        # These direct comparison snippets can also occur in a nested helper.
        line = definition['line']
        offset = sum(map(len, body.splitlines(keepends=True)[:line - function.lineno]))
        before, after = body[:offset], body[offset:]
        assert definition['source'] in after.splitlines()[0]
        body = before + after.replace(definition['source'], definition['mutation'], 1)
    else:
        assert definition['source'] in body, definition['id']
        body = body.replace(definition['source'], definition['mutation'])
    return source[:start] + textwrap.indent(body, indent) + source[end:]


class Campaign:
    def __init__(self, definition, output):
        self.definition = definition
        self.output = output
        self.reports = []

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed:
            self.reports.append({'node': report.nodeid, 'phase': report.when,
                                 'outcome': report.outcome})

    def pytest_sessionfinish(self, session, exitstatus):
        self.output.write_text(json.dumps({
            'id': self.definition['id'], 'exit': int(exitstatus),
            'selected': self.definition['nodes'], 'reports': self.reports}, indent=2) + '\n')


def main():
    definitions = json.loads(MANIFEST.read_text())
    if len(sys.argv) == 4 and sys.argv[1] == '--one':
        import types
        import pytest
        definition = next(d for d in definitions if d['id'] == sys.argv[2])
        # Install before collection, so immediate callers import the mutated
        # functions/classes too. No on-disk module or worktree is rewritten.
        module_name = definition.get('module', 'content_splitter')
        module = types.ModuleType(module_name)
        module.__file__ = str(ROOT / (module_name + '.py'))
        # Dataclass annotation resolution needs its namespace during execution.
        sys.modules[module_name] = module
        exec(compile(mutated_source(definition), module.__file__, 'exec'), module.__dict__)
        return pytest.main(definition['nodes'] + ['-q', '--tb=short', '-p', 'no:cacheprovider'],
                           plugins=[Campaign(definition, Path(sys.argv[3]))])
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    summary = []
    for definition in definitions:
        name = definition['id'].replace(':', '-') or 'empty'
        report = output / (name + '.json')
        with (output / (name + '.log')).open('w') as log:
            result = subprocess.run([sys.executable, __file__, '--one', definition['id'], str(report)],
                                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        data = json.loads(report.read_text()) if report.exists() else {}
        calls = data.get('reports', [])
        caught = (result.returncode == 1 and len(calls) == len(definition['nodes']) and
                  {r['node'] for r in calls} == set(definition['nodes']) and
                  all(r['phase'] == 'call' and r['outcome'] == 'failed' for r in calls))
        summary.append({'id': definition['id'], 'caught_all_selected': caught, **data})
        print(definition['id'], 'caught all selected witnesses' if caught else 'NOT CAUGHT', flush=True)
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if all(r['caught_all_selected'] for r in summary) else 1


if __name__ == '__main__':
    raise SystemExit(main())
