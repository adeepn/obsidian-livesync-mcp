"""Own-write interpretation: C:pouchdb/compress.ts:49-71; reflection.ts:272-283.

Expectations are committed executions of the pinned compressor/reader, with
published dependencies; no expected decoded content is derived from peer code.
"""
import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path

import pytest
import server
from storage_base import StorageError
from storage_couchdb import CouchDBBackend
from storage_journal import JournalBackend, MILESTONE_KEY
from tests.couchdb_fake import make_fake_couchdb
from tests.test_storage_journal import make_backend, healthy_bucket

FIXTURE = json.loads(Path('tests/fixtures/compression_upstream.json').read_text())
VALID = next(v for v in FIXTURE['vectors'] if v['name'] == 'ascii')
INVALID = next(v for v in FIXTURE['vectors'] if v['name'] == 'marked')
NOTICE = {'code':'couchdb_marker_interpreted','chunks':1,'metadata_size_preserved':True}


@asynccontextmanager
async def marker_vault(tmp_path, kind, compression):
    settings = {'pathNormalizationVersion':0, 'handleFilenameCaseSensitive':False,
                'chunkSplitterVersion':'v1', 'minimumChunkSize':1, 'hashAlg':'sha1',
                'enableCompression':compression}
    milestone = {'tweak_values':{'PREFERRED':settings}}
    if kind == 'couchdb':
        transport, log = make_fake_couchdb({'_local/obsydian_livesync_milestone':milestone})
        def new_reader():
            return CouchDBBackend('http://test','user','password','vault',transport=transport)
        backend = new_reader()
        mutations = lambda: sum(r.method in ('PUT','POST','DELETE') for r in log)
    else:
        objects = healthy_bucket()
        objects[MILESTONE_KEY] = json.dumps(milestone).encode()
        backend, storage = make_backend(tmp_path/'writer',objects)
        new_reader = lambda: JournalBackend(storage,tmp_path/'reader')
        mutations = lambda: len(storage.put_calls)
    try:
        await backend.startup_check()
        yield backend, new_reader, mutations
    finally:
        await backend.close()


@pytest.mark.parametrize('kind',['couchdb','journal'])
@pytest.mark.parametrize('compression',[False,True])
async def test_marked_input_notice_repeat_and_fresh_reader(tmp_path,kind,compression):
    async with marker_vault(tmp_path,kind,compression) as (backend,new_reader,mutations):
        original = VALID['compressed']
        first = await backend.write_note('marked.md',original,create=True)
        assert first['size'] == len(original.encode())
        expected = VALID['decompressed'] if kind=='couchdb' else original
        if kind=='couchdb':assert first['notices']==[NOTICE]
        else:assert 'notices' not in first
        assert (await backend.read_note('marked.md'))['text']==expected
        repeated = await backend.write_note('marked.md',original,expected_rev=first['rev'])
        assert repeated['size']==first['size']
        if kind=='couchdb':assert repeated['notices']==[NOTICE]
        reader = new_reader()
        try:
            await reader.startup_check()
            await reader.refresh(force=True)
            note = await reader.read_note('marked.md')
            assert note['text']==expected
            assert 'notices' not in note['meta']
        finally:
            await reader.close()


@pytest.mark.parametrize('compression',[False,True])
@pytest.mark.parametrize('prefix',['','safe\n'])
async def test_undecodable_new_prefix_refuses_before_any_mutation(tmp_path,compression,prefix):
    async with marker_vault(tmp_path,'couchdb',compression) as (backend,_,mutations):
        before = mutations()
        with pytest.raises(StorageError,match='binary attachment path'):
            await backend.write_note('bad.md',prefix+INVALID['input'],create=True)
        assert mutations()==before
        assert await backend.read_note('bad.md') is None
        token = server._active_backend.set(backend)
        try:
            result = await server.obsidian_create_note('bad.md',prefix+INVALID['input'])
            assert 'binary attachment path' in result
            assert 'Created' not in result
            assert mutations()==before
        finally:
            server._active_backend.reset(token)


@pytest.mark.parametrize('upload',[False,True])
async def test_marker_notice_appends_outside_tool_fields(tmp_path,upload):
    async with marker_vault(tmp_path,'couchdb',True) as (backend,_,mutations):
        token=server._active_backend.set(backend)
        try:
            if upload:
                result=await server.obsidian_upload_attachment('marked.md',base64.b64encode(VALID['compressed'].encode()).decode())
                parsed=json.loads(result.splitlines()[0])
                assert parsed['size']==len(VALID['decompressed'].encode())
                assert parsed['sha256']==hashlib.sha256(VALID['decompressed'].encode()).hexdigest()
            else:
                result=await server.obsidian_create_note('marked.md',VALID['compressed'])
                assert result.splitlines()[0].startswith('Created')
            assert json.loads(result.splitlines()[1])=={'notices':[NOTICE]}
            assert 'binary attachment path' in result
            assert 'processSizeMismatchedFiles' in result
            assert VALID['compressed'] not in result and VALID['decompressed'] not in result
        finally:
            server._active_backend.reset(token)


async def test_invalid_marker_update_preserves_existing_revision(tmp_path):
    async with marker_vault(tmp_path,'couchdb',True) as (backend,_,mutations):
        await backend.write_note('stable.md','before',create=True)
        before=await backend.read_note('stable.md')
        count=mutations()
        with pytest.raises(StorageError,match='binary attachment path'):
            await backend.write_note('stable.md',INVALID['input'],expected_rev=before['rev'])
        assert mutations()==count
        assert await backend.read_note('stable.md')==before


async def test_marked_input_still_refuses_different_existing_logical_data(tmp_path):
    async with marker_vault(tmp_path,'couchdb',True) as (backend,_,mutations):
        meta=await backend.write_note('stable.md',VALID['compressed'],create=True)
        doc=await backend._get_doc(meta['doc_id'])
        chunk=doc['children'][0]
        stored=await backend._get_doc(chunk)
        alternate=next(v for v in FIXTURE['vectors'] if v['name']=='unicode')
        await backend._put_doc(chunk,{'_rev':stored['_rev'],'type':'leaf','data':alternate['compressed']})
        before=await backend.read_note('stable.md')
        count=mutations()
        with pytest.raises(StorageError,match='holds different data'):
            await backend.write_note('stable.md',VALID['compressed'],expected_rev=meta['rev'])
        assert mutations()==count
        assert await backend.read_note('stable.md')==before
