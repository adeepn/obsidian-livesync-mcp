"""CouchDB transforms: commonlib src/pouchdb/compress.ts:9-101, RemoteService.ts:457.

Expectations are executions of the pinned upstream and published dependencies,
never calculated by the production codec. Drop marker/~ or change deflate level,
shorter-only, CRLF detection or leaf decoding: these tests must fail.
"""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from storage_base import StorageError, UnsupportedVault
from tests.test_storage_couchdb import fake_vault, stored_doc
from tests.test_tweak_compatibility import FULL

FIXTURE = Path(__file__).parent / 'fixtures/compression_upstream.json'
ORACLE = json.loads(FIXTURE.read_text())
MARKER = ORACLE['marker']
VECTORS = ORACLE['vectors']


@pytest.mark.parametrize('v', VECTORS, ids=lambda v: v['name'])
def test_upstream_compressed_read(v):
    from couchdb_compression import decompress_data
    if v['error'] or v['name'] == 'marked':
        with pytest.raises(StorageError):
            decompress_data(v['compressed'])
    else:
        assert decompress_data(v['compressed']) == v['decompressed']


@pytest.mark.parametrize('v', VECTORS, ids=lambda v: v['name'])
def test_lossless_peer_write_and_form(v):
    from couchdb_compression import compress_data, decompress_data
    x = v['input']
    wire = compress_data(x)
    if not isinstance(x, str):
        assert wire == x
        return
    if x.startswith(MARKER):
        assert wire == x  # compressDoc:52: already marked is never re-encoded.
        return
    assert decompress_data(wire) == x
    lossy = '\r' in x or '\n' in x or any(0xd800 <= ord(c) <= 0xdfff for c in x) or x.startswith('\ufeff')
    if not lossy and v['compressed'] != x:
        assert wire.startswith(MARKER)
    if wire.startswith(MARKER):
        assert len(wire.encode('utf-16-le')) < len(x.encode('utf-16-le'))
        if not lossy:
            assert wire[len(MARKER):].startswith('~') == (v['form'] == 'binary')
    if any(0xd800 <= ord(c) <= 0xdfff for c in x):
        assert wire == x  # Blob USVString would change these pieces (compress.ts:13).
    if x.startswith('\ufeff'):
        assert wire == x  # Blob.text strips a leading BOM (compress.ts:46).
    if v['name'] in ('short', 'empty', 'random'):
        assert wire == x


@pytest.mark.parametrize('v', [v for v in VECTORS if 'zlib' in v], ids=lambda v: v['name'])
def test_upstream_reads_zlib_streams(v):
    from couchdb_compression import decompress_data
    for stream in v['zlib']:
        assert decompress_data(stream['wire']) == stream['result']


@pytest.mark.parametrize('v', ORACLE['invalid_utf8'])
def test_blob_replacement_and_bom_read(v):
    from couchdb_compression import decompress_data
    assert decompress_data(v['wire']) == v['result']  # compress.ts:45-47: Blob.text


def test_empty_marked_payload():
    from couchdb_compression import decompress_data
    for x in (MARKER, MARKER+'~'):
        assert decompress_data(x) == ''  # compress.ts:28-36


@pytest.mark.parametrize('payload', ['private-not-base64!', 'eA==', 'Aw==', '====', 'not🔒base64'])
def test_corrupt_marked_data_is_sanitized(payload):
    from couchdb_compression import decompress_data
    with pytest.raises(StorageError) as caught:
        decompress_data(MARKER+payload)
    assert payload not in str(caught.value)
    assert payload not in caught.value.format()
    assert caught.value.cause_class in {'Error', 'error', 'ValueError', 'UnicodeEncodeError'}
    assert 'compressed' in caught.value.format()
    assert 'restore' in caught.value.format()


def test_raw_level_eight_encoding():
    from couchdb_compression import compress_data
    # Node zlib.deflateRawSync(level=8) bytes, read back by real upstream.
    for v in VECTORS:
        if v['name'] == 'level_witness':
            expected = next(s['wire'] for s in v['zlib'] if s['mode'] == v['form'])
            assert compress_data(v['input']) == expected
            assert expected != v['zlib_level6']
            return
    pytest.fail('missing level witness')


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('bypass', [False, True])
async def test_backend_operations_and_fresh_reader(enabled, bypass, monkeypatch):
    import server
    from storage_couchdb import CouchDBBackend
    milestone = {'tweak_values': {'PREFERRED': {**FULL, 'enableCompression': enabled},
                                 'device': {**FULL, 'enableCompression': not enabled}}}
    async with fake_vault({'_local/obsydian_livesync_milestone': milestone}) as (backend, raw, log):
        backend.disable_checking_config_mismatch = bypass
        await backend.startup_check()
        assert backend.enable_compression is enabled
        assert 'enableCompression' not in (await backend.backend_info())['tweak_assessment']['differing_keys']
        monkeypatch.setattr(server, 'backend', backend)
        info = json.loads(await server.obsidian_vault_stats())
        assert info['enableCompression'] is enabled
        text = 'readable content! Привет 🙂\r\n' * 300
        await backend.write_note('note.md', text, create=True)
        assert (await backend.read_note('note.md'))['text'] == text
        await backend.write_note('note.md', text + 'updated! ' * 100)
        await backend.copy_note('note.md', 'copy.md')
        await backend.rename_note('copy.md', 'renamed.md')
        assert (await backend.read_note('renamed.md'))['text'] == text + 'updated! ' * 100
        # Upload/download exercise the MCP paths over real backend transforms.
        binary = b'\xff\x00binary' * 1000
        encoded = base64.b64encode(binary).decode()
        result = json.loads(await server.obsidian_upload_attachment('file.pdf', encoded))
        assert result['sha256'] == hashlib.sha256(binary).hexdigest()
        assert json.loads(await server.obsidian_download_attachment('file.pdf'))['content_b64'] == encoded
        docs = (await raw.get('obsidian/_all_docs?include_docs=true')).json()['rows']
        leaves = [r['doc'] for r in docs if r['doc'].get('type') == 'leaf']
        assert leaves
        assert any(d['data'].startswith(MARKER) for d in leaves) is enabled
        snapshots = {d['_id']: d for d in leaves}
        second = CouchDBBackend('http://couchdb.test:5984','admin','secret-password','obsidian',
                                transport=raw._transport)
        try:
            await second.startup_check()
            assert (await second.read_note('note.md'))['text'] == text + 'updated! ' * 100
            assert (await second.read_note('file.pdf'))['data'] == binary
        finally:
            await second.close()
        await backend.delete_note('note.md')
        assert await backend.read_note('note.md') is None
        for key, doc in snapshots.items():
            assert await stored_doc(raw, key) == doc
        assert not any(r.method == 'DELETE' for r in log)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('kind', ['notes', 'plain', 'newnote'])
async def test_upstream_preseeded_inline_and_leaf(enabled, kind):
    v = next(v for v in VECTORS if v['name'] == ('base64' if kind == 'newnote' else 'ascii'))
    note = {'type': kind, 'path': 'pre.md', 'size': len(v['input']), 'ctime':1, 'mtime':1}
    docs = {'_local/obsydian_livesync_milestone': {'tweak_values': {'PREFERRED': {**FULL,'enableCompression':enabled}}}}
    if kind == 'notes':
        note['data'] = v['compressed']
    else:
        note['children'] = ['h:pre']
        docs['h:pre'] = {'type':'leaf', 'data':v['compressed']}
    docs['pre.md'] = note
    async with fake_vault(docs) as (backend, raw, log):
        backend.disable_checking_config_mismatch = True
        await backend.startup_check()
        read = await backend.read_note('pre.md')
        if kind == 'newnote':
            assert read['data'] == base64.b64decode(v['input'])
        else:
            assert read['text'] == v['input']
        await backend.copy_note('pre.md', 'copied.md')
        await backend.rename_note('copied.md', 'moved.md')
        assert (await backend.read_note('moved.md'))['text'] == read['text']
        if kind == 'notes':
            assert (await stored_doc(raw, 'moved.md'))['data'].startswith(MARKER) is enabled


@pytest.mark.parametrize('value', [None, 1, 'private-secret', {}, []])
async def test_invalid_compression_setting_is_sanitized(value):
    async with fake_vault({'_local/obsydian_livesync_milestone': {'enableCompression': value}}) as (backend, raw, log):
        with pytest.raises(UnsupportedVault, match='enableCompression must be a boolean'):
            await backend.startup_check()
        assert not any(r.method == 'PUT' for r in log)


@pytest.mark.parametrize('settings,expected', [
    ({'useEden': False}, False),
    ({'tweak_values': {'one': {'enableCompression':True}, 'two':{'enableCompression':False}}}, True),
    ({'tweak_values': {'PREFERRED':{'enableCompression':False}, 'one':{'enableCompression':True}}}, False),
])
async def test_setting_resolution(settings, expected):
    async with fake_vault({'_local/obsydian_livesync_milestone':settings}) as (backend, raw, log):
        await backend.startup_check()
        assert backend.enable_compression is expected


@pytest.mark.parametrize('kind', ['plain', 'newnote', 'leaf', 'notes'])
async def test_transform_applies_to_every_string_data_document(kind):
    # compress.ts:49-71 tests only the data type, never the document type.
    v = next(v for v in VECTORS if v['name'] == 'ascii')
    async with fake_vault({'_local/obsydian_livesync_milestone': {'enableCompression': True}}) as (backend, raw, log):
        await backend.startup_check()
        await backend._put_doc('h:direct', {'type':kind, 'data':v['input']})
        assert (await stored_doc(raw, 'h:direct'))['data'].startswith(MARKER)
        assert (await backend._get_doc('h:direct'))['data'] == v['input']
        assert (await backend._post_all_docs(['h:direct'], include_docs=True))[0]['doc']['data'] == v['input']


async def test_compressed_chunk_reuse_and_collision_preserve_documents():
    from content_splitter import chunk_id_for_piece
    v = next(v for v in VECTORS if v['name'] == 'ascii')
    key = chunk_id_for_piece(v['input'])
    docs = {key:{'type':'leaf','data':v['compressed']},
            '_local/obsydian_livesync_milestone':{'enableCompression':True}}
    async with fake_vault(docs) as (backend, raw, log):
        await backend.startup_check()
        before = await stored_doc(raw, key)
        assert await backend._write_chunk('note.md', v['input']) == key
        assert await stored_doc(raw, key) == before
        assert not any(r.method == 'PUT' for r in log)
    docs[key]['data'] = next(v for v in VECTORS if v['name'] == 'unicode')['compressed']
    async with fake_vault(docs) as (backend, raw, log):
        await backend.startup_check()
        before = await stored_doc(raw, key)
        with pytest.raises(StorageError):
            await backend._write_chunk('note.md', v['input'])
        assert await stored_doc(raw, key) == before
        assert not any(r.method == 'PUT' for r in log)


async def test_corrupt_compressed_source_refuses_before_any_mutation(monkeypatch):
    import server
    docs = {'bad.md': {'type':'notes','path':'bad.md','data':MARKER+'private-invalid!',
                       'size':100,'ctime':1,'mtime':1}}
    async with fake_vault(docs) as (backend, raw, log):
        backend.disable_checking_config_mismatch = True
        await backend.startup_check()
        monkeypatch.setattr(server, 'backend', backend)
        for operation in (lambda: backend.read_note('bad.md'),
                          lambda: backend.copy_note('bad.md','copy.md'),
                          lambda: backend.rename_note('bad.md','move.md'),
                          lambda: backend.delete_note('bad.md'),
                          lambda: backend.write_note('bad.md','replacement')):
            with pytest.raises(StorageError):
                await operation()
        result = await server.obsidian_fetch('bad.md')
        assert 'private-invalid!' not in result
        assert 'restore' in result
        assert not any(r.method == 'PUT' for r in log)


@pytest.mark.parametrize('enabled', [False, True])
def test_effective_tweaks_advertise_couchdb_writer(enabled):
    from livesync_format import _effective_peer_tweaks
    assert _effective_peer_tweaks({'enableCompression':enabled}, couchdb_mode=True)['enableCompression'] is enabled


@pytest.mark.parametrize('value', [1, 'private', None, {'private':True}])
async def test_stats_compression_field_is_typed(monkeypatch, value):
    import server
    from tests.fake_backend import FakeBackend
    backend = FakeBackend()
    async def info():
        return {'backend':'couchdb', 'enableCompression':value}
    monkeypatch.setattr(backend, 'backend_info', info)
    monkeypatch.setattr(server, 'backend', backend)
    result = json.loads(await server.obsidian_vault_stats())
    assert result['enableCompression'] is False
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('v', [v for v in VECTORS if isinstance(v['input'], str)], ids=lambda v: v['name'])
def test_upstream_binary_text_choice_even_when_result_is_not_shorter(v, monkeypatch):
    import couchdb_compression as codec
    x = v['input']
    if (not x or x.startswith((MARKER, '\ufeff')) or '\r' in x or '\n' in x
            or any(0xd800 <= ord(c) <= 0xdfff for c in x)):
        return
    actual = []
    factory = codec.zlib.compressobj
    class Encoder:
        def __init__(self, *args, **kwargs):
            self.encoder = factory(*args, **kwargs)
        def compress(self, data):
            actual.append(base64.b64encode(data).decode())
            return self.encoder.compress(data)
        def flush(self):
            return self.encoder.flush()
    monkeypatch.setattr(codec.zlib, 'compressobj', Encoder)
    codec.compress_data(x)
    assert actual == [v['encoding_bytes_b64']]
