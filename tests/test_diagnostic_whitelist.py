"""P-19: origin-aware diagnostics; reviewer corpus is data, not an oracle."""
import asyncio
import inspect
import html
import re
import unicodedata
from urllib.parse import unquote
import json
import time
from pathlib import Path

import pytest
import server
import storage_base as errors
from diagnostic_hygiene import sanitize_diagnostic
from tests.fake_backend import FakeBackend
from tests.test_storage_messages import descendants

CORPUS = json.loads((Path(__file__).parent / 'fixtures/diagnostic_attack_corpus.json').read_text())


def assert_no_remote_secret(output, case):
    """Inspect residual encodings as well as every six-character secret window."""
    def canonical(value):
        value = unicodedata.normalize('NFKC', html.unescape(unquote(value)))
        return ''.join(c for c in value if c not in '\u200b\u00ad\u0301\x00')
    normalized = canonical(output)
    secret = canonical(case['secret'])
    assert secret not in normalized
    for i in range(max(0, len(secret) - 5)):
        assert secret[i:i+6] not in normalized
    assert not re.search(r'[0-9a-fA-F]{32,}', output)
    assert not re.search(r'(?:AKIA|ASIA)[A-Z0-9]{16}', output)
    assert not re.search(r'(?:ghp_|gho_|sk-|xoxb-|AIza)[A-Za-z0-9_-]{20,}', output)
    assert not re.search(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', output)
    assert '-----BEGIN' not in output
    assert not re.search(r'[A-Za-z0-9+/=_-]{40,}', output)


def escape(value):
    return ''.join({'\n': r'\n', '\r': r'\r', '\t': r'\t'}.get(c,
        f'\\u{ord(c):04x}' if ord(c) < 32 or 127 <= ord(c) <= 159 or c in '\u2028\u2029' else c) for c in value)


@pytest.mark.parametrize('case', CORPUS, ids=lambda c: c['id'])
async def test_remote_exception_corpus_all_routes(monkeypatch, case):
    b = FakeBackend()
    monkeypatch.setattr(server, 'backend', b)
    for kind in descendants(errors.StorageError):
        e = kind.__new__(kind)
        Exception.__init__(e, case['payload'])
        e.__dict__.update(path=case['payload'], source=case['payload'], destination=case['payload'],
                          size=1, maximum=2, source_state='live', detail=case['payload'], rule=case['payload'])
        outputs = [server._storage_message(e)]
        b.errors['refresh'] = e
        for handler in server._tool_handlers:
            kw = {n:'note.md' for n,p in inspect.signature(handler).parameters.items() if p.default is inspect.Parameter.empty}
            actions = {'obsidian_update_note': ('mode', ['overwrite','append','prepend','after-heading','search-replace']),
                       'obsidian_manage_properties': ('action',['get','set','delete']),
                       'obsidian_manage_tags': ('action',['list','add','remove']),
                       'obsidian_canvas_node': ('action',['add','remove']),
                       'obsidian_canvas_edge': ('action',['add','remove'])}.get(handler.__name__, (None,[None]))
            for action in actions[1]:
                args = dict(kw)
                if actions[0]: args[actions[0]] = action
                outputs.append(await handler(**args))
        for output in outputs:
            assert_no_remote_secret(output, case)


@pytest.mark.parametrize('path', [c['payload'] for c in CORPUS] + ['password: ordinary.md', 'a'+'1'*64+'.md', 'ＢＥＡＲＥＲ note.md', 'ak:sk@host.md', 'e\u0301.md'])
async def test_caller_path_identity(monkeypatch, path):
    b = FakeBackend()
    monkeypatch.setattr(server, 'backend', b)
    b.errors['refresh'] = errors.WriteSkipped('remote-path', 2*1024**2, 1)
    result = json.loads(await server.obsidian_create_note(path, 'body'))
    assert result['path'] == escape(path)
    assert escape(path) in result['message']
    b.errors['refresh'] = errors.OutcomeUnknown('remote detail')
    result = await server.obsidian_create_note(path, 'body')
    assert escape(path) in result
    assert 'MAY have been applied' in result
    b.errors.clear()
    result = await server.obsidian_fetch(path)
    assert escape(path) in result


@pytest.mark.parametrize('size', [1024, 100_000, 1_000_000])
@pytest.mark.parametrize('char', ['a', '.', '%', '{', '%2525', '{\"password\":'])
async def test_last_line_event_loop_budget(size, char):
    value = char * size
    start = time.perf_counter()
    result = sanitize_diagnostic(value)
    elapsed = time.perf_counter() - start
    assert elapsed < .05
    assert len(result) <= 4096
    await asyncio.sleep(0)


@pytest.mark.parametrize('case', CORPUS, ids=lambda c:c['id'])
@pytest.mark.parametrize('kind', ['couchdb','journal'])
async def test_real_backend_diagnostic_projection(tmp_path, monkeypatch, case, kind):
    from tests.test_tweak_startup import vault
    async with vault(tmp_path, kind, {'tweak_values': {'PREFERRED': {'pathNormalizationVersion':0}}}) as (b, _):
        await b.startup_check()
        monkeypatch.setattr(server, 'backend', b)
        b._writer_unsupported_reason = case['payload']
        b._tweak_assessment = {'status':case['payload'], 'differing_keys':[case['payload']]}
        outputs = [json.dumps(await b.backend_info()), await server.obsidian_create_note('note.md','body'),
                   await server.obsidian_vault_stats()]
        for out in outputs:
            assert_no_remote_secret(out, case)


@pytest.mark.parametrize('case', CORPUS, ids=lambda c:c['id'])
async def test_real_replay_hostile_name(tmp_path, case):
    from tests.test_storage_journal import make_backend, MILESTONE_KEY, SYNC_PARAMETERS_KEY, params_blob
    from diagnostic_hygiene import journal_name
    name = '2-hostile-' + case['payload'] + '-docs.jsonl.gz'
    b, _ = make_backend(tmp_path, {MILESTONE_KEY: b'{"pathNormalizationVersion":0}',
                                  SYNC_PARAMETERS_KEY:params_blob(), name:b'not gzip'})
    try:
        await b.startup_check()
        with pytest.raises(errors.StorageError) as caught:
            await b.refresh(force=True)
        out = server._storage_message(caught.value)
        assert_no_remote_secret(out, case)
        assert journal_name(name) in out
        assert 'cause:' in out
    finally:
        await b.close()


@pytest.mark.parametrize('status', [401,403,500])
async def test_couchdb_log_hygiene(caplog, status, monkeypatch):
    import httpx
    from storage_couchdb import CouchDBBackend
    monkeypatch.delenv('LIVESYNC_UNSAFE_DEBUG', raising=False)
    b = CouchDBBackend('http://fake','user','password','vault', transport=httpx.MockTransport(
        lambda r: httpx.Response(status,json={'error':'Authorization: Bearer LogSecretMarker'})))
    try:
        with pytest.raises(errors.StorageError):
            await b._request('GET','vault/a.md')
        assert 'LogSecretMarker' not in caplog.text
        assert str(status) in caplog.text
    finally:
        await b.close()


def test_invalid_selection_log_hygiene(caplog, monkeypatch):
    from selection_engine import validate_response
    monkeypatch.delenv('LIVESYNC_UNSAFE_DEBUG', raising=False)
    pattern='secretAccessKey=PatternSecretMarker['
    validate_response(dict(allowed=False, rule=None, writer_supported=False, reason=pattern))
    assert pattern not in caplog.text
    assert 'PatternSecretMarker' not in caplog.text


def test_remote_path_normalization_and_journal_grammar():
    from diagnostic_hygiene import DiagnosticPath, journal_name, setting_key
    assert DiagnosticPath('e\u0301\n.md',remote=True).format() == 'é\\n.md'
    assert DiagnosticPath('e\u0301\n.md',remote=False).format() == 'e\u0301\\n.md'
    assert journal_name('123-docs.jsonl.gz') == '123-docs.jsonl.gz'
    name='123-'+'a'*32+'-docs.jsonl.gz'
    assert journal_name(name) == name
    assert journal_name('123-secret-docs.jsonl.gz') == '<unexpected journal name> (length 24)'
    assert setting_key('remote-key') == '<unexpected key>'


@pytest.mark.parametrize('case', CORPUS, ids=lambda c:c['id'])
async def test_unexpected_exception_boundary(monkeypatch, case):
    b = FakeBackend()
    monkeypatch.setattr(server, 'backend', b)
    b.errors['refresh'] = ValueError(case['payload'])
    out = await server.obsidian_create_note('note.md','body')
    assert_no_remote_secret(out, case)
    assert 'ValueError' in out
    assert 'note.md' in out


@pytest.mark.parametrize('case', CORPUS, ids=lambda c:c['id'])
async def test_caller_echo_routes(monkeypatch, case):
    p = case['payload']
    b = FakeBackend()
    monkeypatch.setattr(server, 'backend', b)
    b.add('safe.md', text='# real heading\nbody')
    b.add('safe.canvas', text=json.dumps({'nodes':[{'id':'a','type':'text','text':'a'},{'id':'b','type':'text','text':'b'}],'edges':[]}))
    calls = [
        lambda:server.obsidian_fetch(p), lambda:server.obsidian_download_attachment(p),
        lambda:server.obsidian_update_note(p,'append','body'), lambda:server.obsidian_delete_note(p),
        lambda:server.obsidian_manage_properties(p,'get'), lambda:server.obsidian_manage_tags(p,'list'),
        lambda:server.obsidian_canvas_node(p), lambda:server.obsidian_canvas_edge(p),
        lambda:server.obsidian_create_from_template(p,'target.md'),
        lambda:server.obsidian_update_note('safe.md',p,'body'),
        lambda:server.obsidian_manage_properties('safe.md',p), lambda:server.obsidian_manage_tags('safe.md',p),
        lambda:server.obsidian_canvas_node('safe.canvas',p), lambda:server.obsidian_canvas_edge('safe.canvas',p),
        lambda:server.obsidian_update_note('safe.md','after-heading','body',heading=p),
        lambda:server.obsidian_canvas_node('safe.canvas','remove',node_id=p),
        lambda:server.obsidian_canvas_edge('safe.canvas','remove',edge_id=p),
        lambda:server.obsidian_canvas_edge('safe.canvas',from_node=p,to_node='b'),
    ]
    for call in calls:
        assert escape(p) in await call()


def test_debug_opt_in_is_explicit_and_redacted(caplog, monkeypatch):
    import logging
    from diagnostic_hygiene import unsafe_debug
    logger = logging.getLogger('diagnostic-debug-test')
    with caplog.at_level(logging.DEBUG):
        monkeypatch.delenv('LIVESYNC_UNSAFE_DEBUG', raising=False)
        unsafe_debug(logger, 'Authorization: Bearer DebugSecretMarker')
        assert caplog.records == []
        monkeypatch.setenv('LIVESYNC_UNSAFE_DEBUG','1')
        unsafe_debug(logger, 'Authorization: Bearer DebugSecretMarker')
        assert 'unsafe diagnostic: [redacted]' in caplog.text
        assert 'DebugSecretMarker' not in caplog.text


@pytest.mark.parametrize('case', CORPUS, ids=lambda c:c['id'])
@pytest.mark.parametrize('kind', ['couchdb','journal'])
async def test_real_setting_values_hidden(tmp_path, case, kind, monkeypatch):
    from tests.test_tweak_startup import vault
    async with vault(tmp_path, kind, {'tweak_values':{'PREFERRED':{'hashAlg':case['payload']}}}) as (b, _):
        await b.startup_check()
        monkeypatch.setattr(server,'backend',b)
        out = json.dumps(await b.backend_info()) + await server.obsidian_create_note('note.md','body')
        assert_no_remote_secret(out, case)


async def test_backend_config_and_remote_observability_are_not_text(tmp_path):
    from tests.test_storage_journal import make_backend, healthy_bucket, params_blob, SYNC_PARAMETERS_KEY, journal_blob, meta_doc, JOURNAL_1
    from tests.test_storage_couchdb import fake_backend
    from diagnostic_hygiene import fingerprint
    marker='ConfigAndMetadataSecretMarker'
    b, _ = make_backend(tmp_path/marker, healthy_bucket(**{
        SYNC_PARAMETERS_KEY:params_blob(salt=marker),
        JOURNAL_1:journal_blob([{**meta_doc('note.md','1-a'),marker:'value'}]),
        '2-docs.jsonl.gz':None, '3-docs.jsonl.gz':None,
    }))
    try:
        await b.startup_check()
        await b.refresh(force=True)
        info=await b.backend_info()
        assert marker not in json.dumps(info)
        assert info['epoch'] == fingerprint('2:'+marker)
        assert info['protocol_notes'] == ['unknown metadata fields in recent journals: 1']
    finally:
        await b.close()
    async with fake_backend({}) as (b, _):
        b._database=marker
        async def info():
            return {'doc_count':marker,'update_seq':marker}
        b._db_info=info
        assert marker not in json.dumps(await b.backend_info())
