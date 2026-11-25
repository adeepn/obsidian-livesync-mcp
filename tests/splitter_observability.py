"""Isolated, in-memory comparison mutations; never rewrite production files."""
import ast
from pathlib import Path
import types

SOURCE_PATH = Path(__file__).resolve().parents[1] / 'content_splitter.py'


def comparison_sites(source):
    """Include chained comparisons as one site, with every endpoint retained."""
    tree = ast.parse(source)
    sites = []

    def visit(node, scope=''):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = scope + '.' + node.name if scope else node.name
        if isinstance(node, ast.Compare):
            for operator in range(len(node.ops)):
                sites.append((node.lineno, node.col_offset, scope,
                              ast.get_source_segment(source, node), operator))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)
    visit(tree)
    return sorted(sites)


class MutationFuelExhausted(RuntimeError):
    pass


def equality_mutation(source, operator):
    node = ast.parse(source, mode='eval').body
    flip = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE,
            ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
            ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn,
            ast.NotIn: ast.In}
    operation = type(node.ops[operator])
    if operation in (ast.Eq, ast.Is, ast.In):
        return 'False'  # Remove only the matching equality/membership case.
    if operation in (ast.NotEq, ast.IsNot, ast.NotIn):
        return 'True'  # Admit only the excluded equality/membership case.
    node.ops[operator] = flip[operation]()
    return ast.unparse(node)


def isolated_mutant(entry):
    source = SOURCE_PATH.read_text()
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[:entry['line'] - 1])) + entry['column']
    assert source[start:start + len(entry['source'])] == entry['source']
    source = source[:start] + entry['mutation'] + source[start + len(entry['source']):]
    tree = ast.parse(source)
    # A sentinel flip can prevent progress. Inject deterministic fuel into
    # isolated loops only; baseline production and files remain untouched.
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            node.body.insert(0, ast.Expr(ast.Call(
                ast.Name('_mutation_tick', ast.Load()), [], [])))
    ast.fix_missing_locations(tree)
    module = types.ModuleType('isolated_splitter')
    fuel = [1000]

    def tick():
        fuel[0] -= 1
        if fuel[0] <= 0:
            raise MutationFuelExhausted('isolated mutation exhausted loop fuel')
    module._mutation_fuel = fuel
    module._mutation_tick = tick
    module.__file__ = str(SOURCE_PATH)
    exec(compile(tree, str(SOURCE_PATH), 'exec'), module.__dict__)
    return module


def grid_result(module, vector):
    from tests.settings_oracle_inputs import grid_path, grid_payload, is_text_blob
    kind = vector['payload']
    peer = module.ContentSplitter(vector['settings'])
    pieces = peer.split(grid_payload(kind)[1], path=grid_path(kind),
                        text_blob=is_text_blob(kind))
    return pieces, [peer.chunk_id_for_piece(piece) for piece in pieces]


def helper_result(module, vector):
    import base64
    if 'text' in vector:
        return base64.b64encode(module._write_string(vector['text'])).decode()
    return module._base36(vector['value']), module._base36(module._int32(vector['value']))


def coercion_result(module, row):
    import base64
    raw = base64.b64decode(row['payload']['base64'])
    try:
        splitter = module.ContentSplitter(row['settings'])
        pieces = splitter.split(raw, path=row['payload']['path'], text_blob=row['payload']['text'])
        return pieces, [splitter.chunk_id_for_piece(p) for p in pieces]
    except ValueError:
        return ('refused',)
