import pytest
import livesync_format as fmt


def test_parser_preserves_upstream_delimiter():
    assert fmt.parse_custom_regexp_list(' a,b\n|[]|"x"|[]||[]|a,b ') == ('a,b', '"x"', 'a,b')


@pytest.mark.parametrize('path,only,ignore,case,allowed', [
    ('x.md','','',False,True), ('x.md',r'\.md$','',False,True),
    ('x.txt',r'\.md$','',False,False), ('x.md','md','md',False,False),
    ('X.MD','md','',False,True), ('X.MD','md','',True,False),
    ('é.md',r'\w+\.md','',False,False), ('١',r'\d','',False,False),
    ('ab',r'(?<=a)b','',True,True), ('x','!!x','',False,False),
    ('a/b',r'/a/b/','',False,False), ('x','[','',False,None),
])
def test_filter(path, only, ignore, case, allowed):
    verdict = fmt.is_target_file(path, only_patterns=only, ignore_patterns=ignore, case_sensitive=case)
    if allowed is None:
        assert not verdict.writer_supported
    else:
        assert verdict.writer_supported
        assert verdict.allowed == allowed

from tests.test_storage_couchdb import fake_backend
from tests.test_storage_journal import make_backend, healthy_bucket
from storage_base import StorageError


@pytest.fixture(params=['couchdb', 's3'])
async def backend(request, tmp_path):
    if request.param == 'couchdb':
        async with fake_backend() as (b, log):
            yield b, lambda: sum(r.method in ('PUT','DELETE') for r in log)
    else:
        b, storage = make_backend(tmp_path, healthy_bucket())
        await b.startup_check()
        yield b, lambda: len(storage.put_calls)
        await b.close()


@pytest.mark.parametrize('operation', ['create','update','delete','rename','copy','attachment'])
async def test_excluded_writes_never_mutate(backend, operation):
    b, mutations = backend
    await b.write_note('excluded.md', 'old', create=True)
    b.selection_ignore = 'excluded|[]|destination'
    before = mutations()
    with pytest.raises(StorageError, match='syncIgnoreRegEx'):
        if operation == 'create': await b.write_note('destination.md','new',create=True)
        elif operation == 'update': await b.write_note('excluded.md','new')
        elif operation == 'delete': await b.delete_note('excluded.md')
        elif operation == 'rename': await b.rename_note('excluded.md','destination.md')
        elif operation == 'copy': await b.copy_note('excluded.md','destination.md')
        else: await b.write_attachment('destination.png', b'new', create=True)
    assert mutations() == before


async def test_ignore_content_loaded_from_backend(backend):
    b, mutations = backend
    await b.write_note('rules.txt', 'private/**\n!private/keep.md', create=True)
    b.selection_use_ignore_files = True
    b.selection_ignore_files = ('rules.txt',)
    before = mutations()
    with pytest.raises(StorageError, match='ignoreFiles'):
        await b.write_note('private/no.md','new',create=True)
    assert mutations() == before
    await b.write_note('private/keep.md','new',create=True)
    await b.write_note('rules.txt', 'other/**')
    await b.refresh(force=True)
    await b.write_note('private/no.md','new',create=True)

from config import load_config, ConfigError


def test_selection_env():
    cfg = load_config({'LIVESYNC_BACKEND':'couchdb', 'COUCHDB_USER':'admin',
        'COUCHDB_PASSWORD':'real-secret', 'COUCHDB_DATABASE':'vault', 'MCP_API_KEY':'real-key',
        'LIVESYNC_SYNC_ONLY_REGEX':'md|[]|txt', 'LIVESYNC_SYNC_IGNORE_REGEX':'secret',
        'LIVESYNC_USE_IGNORE_FILES':'true', 'LIVESYNC_IGNORE_FILES':'.gitignore, rules.txt'})
    assert cfg.selection_only == 'md|[]|txt'
    assert cfg.selection_ignore == 'secret'
    assert cfg.selection_use_ignore_files
    assert cfg.selection_ignore_files == ('.gitignore','rules.txt')


@pytest.mark.parametrize('key,value', [('LIVESYNC_USE_IGNORE_FILES','perhaps')])
def test_bad_selection_env(key,value):
    with pytest.raises(ConfigError, match=key):
        load_config({'LIVESYNC_BACKEND':'couchdb', 'COUCHDB_USER':'admin',
            'COUCHDB_PASSWORD':'real-secret', 'COUCHDB_DATABASE':'vault', 'MCP_API_KEY':'real-key',
            key:value})


def test_pinned_upstream_differential():
    import json
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root / 'tests/fixtures/selection.json').read_text())
    for row in fixture['parser']:
        assert list(fmt.parse_custom_regexp_list(row['source'])) == row['items']
    inputs = [dict(row) for row in fixture['regex']]
    inputs += [dict(path=row['path'], only_patterns='', ignore_patterns='',
                    ignore_files=row['files'], ignore_names=row['names']) for row in fixture['ignores']]
    result = subprocess.run(['node', str(root / 'vendor/selection/runner.mjs')],
                            input=json.dumps(inputs), text=True, capture_output=True, check=True)
    outputs = json.loads(result.stdout)
    for row, actual in zip(fixture['regex'] + fixture['ignores'], outputs, strict=True):
        assert actual['writer_supported'] == (not row.get('error', False)), row
        if actual['writer_supported']:
            assert actual['allowed'] == row['allowed'], row


async def test_rename_from_regex_excluded_retains_source(backend):
    b, _ = backend
    await b.write_note('old.md', 'old', create=True)
    b.selection_ignore = '^old'
    await b.rename_note('old.md', 'new.md')
    assert await b.read_note('old.md') is not None
    assert await b.read_note('new.md') is not None


async def test_hidden_ignore_file_and_nested_precedence(backend):
    b, mutations = backend
    # Seed ordinary vault metadata directly; public tools intentionally forbid dotfiles.
    for path, text in [('.gitignore', '**/*.md'), ('folder/.gitignore', '!keep.md')]:
        import base64
        prefixed = 'i:' + path
        encoded = base64.b64encode(text.encode()).decode()
        chunk = fmt.chunk_id_for(encoded)
        doc = dict(_id=prefixed, path=prefixed, type='newnote', ctime=1, mtime=1,
                   size=len(text), children=[chunk], eden={chunk:encoded})
        if hasattr(b, '_put_doc'):
            await b._put_doc(prefixed, doc)
        else:
            doc['_rev'] = '1-ignore'
            b._store.apply_journal('seed-' + path, {}, [doc])
    b.selection_use_ignore_files = True
    b.selection_ignore_files = ('.gitignore',)
    before = mutations()
    with pytest.raises(StorageError, match='ignoreFiles'):
        await b.write_note('folder/no.md', 'no', create=True)
    assert mutations() == before
    await b.write_note('folder/keep.md', 'yes', create=True)


async def test_backend_info_reports_invalid_regex(backend):
    b, _ = backend
    b.selection_only = '['
    info = await b.backend_info()
    assert info['filters_active']
    assert not info['writer_supported']
    assert 'pattern' in info['writer_unsupported_reason']


def test_engine_missing_fails_closed(monkeypatch):
    import subprocess
    def missing(*args, **kwargs):
        raise FileNotFoundError('node')
    monkeypatch.setattr(subprocess, 'run', missing)
    assert fmt.is_target_file('x', only_patterns='x').writer_supported is False
    assert fmt.is_target_file('x').allowed


async def test_rename_from_ignore_file_excluded_retains_source(backend):
    b, _ = backend
    await b.write_note('old.md', 'old', create=True)
    await b.write_note('rules.txt', 'old.md', create=True)
    b.selection_use_ignore_files = True
    b.selection_ignore_files = ('rules.txt',)
    await b.rename_note('old.md', 'new.md')
    assert await b.read_note('old.md') is not None
    assert await b.read_note('new.md') is not None


@pytest.mark.parametrize('path', ['/secret/a.md', 'secret\\b.md', 'ok/../secret/a.md'])
async def test_path_alias_cannot_bypass_selection(backend, path):
    b, mutations = backend
    b.selection_ignore = '^secret/'
    before = mutations()
    with pytest.raises(StorageError, match='syncIgnoreRegEx'):
        await b.write_note(path, 'orphan', create=True)
    assert mutations() == before


async def test_ignore_refresh_does_not_accumulate_historical_paths(backend, monkeypatch):
    b, _ = backend
    b.selection_use_ignore_files = True
    b.selection_ignore_files = ('rules.txt',)
    for i in range(3):
        await b.write_note(f'folder{i}/note.md', 'yes', create=True)
    calls = []
    async def read(path):
        calls.append(path)
        return None
    monkeypatch.setattr(b, '_read_selection_file', read)
    await b.refresh_selection_files()
    assert calls == ['rules.txt']
