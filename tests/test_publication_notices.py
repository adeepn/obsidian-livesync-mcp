"""Blob/legacy loss: common/utils.ts:100-115, chunks.ts:218-254,350-370;
reflection.ts:272-283 preserves the size mismatch refusal (P-11).
"""
import json
import pytest
import server
from tests.test_settings_matrix import matrix_vault

@pytest.mark.parametrize('kind', ['couchdb','journal'])
@pytest.mark.parametrize('version', ['v1','v2','v2-segmenter','v3-rabin-karp'])
async def test_bom_notice_only_for_legacy(tmp_path,kind,version):
    settings={'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False,
              'chunkSplitterVersion':version,'hashAlg':'sha1'}
    async with matrix_vault(tmp_path,kind,settings) as (backend,*_):
        await backend.startup_check()
        token=server._active_backend.set(backend)
        @server._tool_errors(writer=True)
        async def publish():
            meta=await backend.write_note('bom.md','\ufeffABC',create=True)
            assert meta['size']==6
            if version!='v3-rabin-karp':
                assert meta['notices']==[{'code':'legacy_bom_removed','removed_bytes':3,'metadata_size_preserved':True}]
            return '{"status":"ok","message":{"code":7},"other":[1,null]}'
        try:
            result=await publish()
            assert json.loads(result.splitlines()[0])=={'status':'ok','message':{'code':7},'other':[1,None]}
            if version!='v3-rabin-karp':
                assert json.loads(result.splitlines()[1])['notices'][0]['code']=='legacy_bom_removed'
                assert 'processSizeMismatchedFiles' in result
                assert (await backend.read_note('bom.md'))['text']=='ABC'
            else:
                assert len(result.splitlines())==1
                assert (await backend.read_note('bom.md'))['text']=='\ufeffABC'
        finally:server._active_backend.reset(token)

@pytest.mark.parametrize('kind', ['couchdb','journal'])
@pytest.mark.parametrize('data,kwargs,code', [(b'A\xffB',{'lossy_text':True},'invalid_utf8_replaced'),
                                           ('A\ud800B',{},'input_surrogates_replaced')])
async def test_other_ingress_loss_is_reported(tmp_path,kind,data,kwargs,code):
    async with matrix_vault(tmp_path,kind,{'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False}) as (backend,*_):
        await backend.startup_check()
        meta=await backend.write_note('loss.md',data,create=True,**kwargs)
        assert meta['notices'][0]['code']==code
        assert (await backend.read_note('loss.md'))['text']=='A\ufffdB'


def test_notice_suffix_does_not_change_existing_fields():
    # Includes collisions with every dedicated reporting key as well as message objects.
    from publication_notices import notice_text
    original='{"message":{"code":7},"notices":["original"],"transport_replacements":77,"other":[1,null]}'
    notice={'code':'journal_surrogates_replaced','count':1}
    result=notice_text(original,[notice])
    assert result.splitlines()[0]==original
    assert json.loads(result.splitlines()[1])=={'notices':[notice]}

@pytest.mark.parametrize('kind', ['couchdb','journal'])
@pytest.mark.parametrize('tool', ['create','overwrite','append','prepend','after-heading','search-replace',
                                  'upload','canvas','properties','tags','template'])
async def test_every_write_tool_exposes_typed_notice(tmp_path,kind,tool):
    import base64
    settings={'pathNormalizationVersion':0,'handleFilenameCaseSensitive':False,'chunkSplitterVersion':'v2','hashAlg':'sha1'}
    async with matrix_vault(tmp_path,kind,settings) as (backend,*_):
        await backend.startup_check()
        await backend.write_note('edit.md','# Anchor\nold',create=True)
        await backend.write_note('template.md','{{value}}',create=True)
        token=server._active_backend.set(backend)
        try:
            if tool=='create':result=await server.obsidian_create_note('new.md','\ufeffABC')
            elif tool=='upload':result=await server.obsidian_upload_attachment('new.md',base64.b64encode(b'\xff').decode(),lossy_text=True)
            elif tool=='canvas':result=await server.obsidian_canvas_node('new.canvas',text='\ud800')
            elif tool in ('properties','tags'):
                from content_splitter import ContentSplitter
                backend._splitter=ContentSplitter({**settings,'chunkSplitterVersion':'v1','customChunkSize':-1,'minimumChunkSize':-1})
                if tool=='properties':result=await server.obsidian_manage_properties('edit.md','set',json.dumps({'x':'🙂'}))
                else:result=await server.obsidian_manage_tags('edit.md','add','🙂')
            elif tool=='template':result=await server.obsidian_create_from_template('template.md','new.md',json.dumps({'value':'\ud800'}))
            elif tool=='search-replace':result=await server.obsidian_update_note('edit.md',tool,search='old',replace='\ud800')
            else:result=await server.obsidian_update_note('edit.md',tool,content='\ud800',**({'heading':'Anchor'} if tool=='after-heading' else {}))
            records=[json.loads(line) for line in result.splitlines() if line.startswith('{"notices":')]
            if tool in ('properties','tags') and kind=='couchdb':
                # Lossless CouchDB rejoins the split UTF-16 pair; no notice.
                assert not records
                assert '🙂' in (await backend.read_note('edit.md'))['text']
                return
            assert len(records)==1, result
            assert records[0]['notices']
            for notice in records[0]['notices']:
                assert set(notice) <= {'code','count','invalid_bytes','removed_bytes','metadata_size_preserved'}
                assert all(type(v) in (int,bool) for k,v in notice.items() if k!='code')
        finally:server._active_backend.reset(token)


def test_legacy_publication_wrapper_preserves_non_string_message():
    # Gate reproducer; JSON fields must remain byte-for-byte intact.
    original='{"status":"ok","message":{"code":7},"other":[1,null]}'
    result=server._publication_text(original,[1])
    assert result.splitlines()[0]==original
    assert json.loads(result.splitlines()[1])=={'notices':[{'code':'journal_surrogates_replaced','count':1}]}


@pytest.mark.parametrize('original', [
    ' \n{"x":1} \n',
    '\n\n',
    ' \tplain text\nsecond line\r\n ',
    '{\n  "message": {"code": 7},\n  "notices": ["original"]\n}\n',
    '',
])
@pytest.mark.parametrize('compatibility', [False, True])
def test_notice_suffix_preserves_result_verbatim(original, compatibility):
    # common/utils.ts:100-115 and journalRecords.ts:69-75 motivate the
    # transformation notice; this peer-owned suffix must not rewrite a result.
    from publication_notices import notice_text
    notice = {'code': 'journal_surrogates_replaced', 'count': 1}
    result = (server._publication_text(original, [1]) if compatibility else
              notice_text(original, [notice]))
    assert result[:len(original)] == original
    suffix = result[len(original):]
    assert suffix.startswith('\n')
    assert json.loads(suffix.splitlines()[1]) == {'notices': [notice]}


@pytest.mark.parametrize('original', [None, {'message': 7}, ['original'], 7])
def test_notice_suffix_preserves_non_string_results(original):
    from publication_notices import notice_text
    assert notice_text(original, [{'code': 'journal_surrogates_replaced',
                                   'count': 1}]) is original
    assert server._publication_text(original, [1]) is original


@pytest.mark.parametrize('invalid', [True, False, 1.0, 2.5, '1', None,
                                      [1], {'count': 1}, [[True]], 0, -1])
def test_compatibility_publication_count_requires_exact_positive_int(invalid):
    # Counts are peer-owned typed diagnostic fields, never JSON coercions.
    original = ' \noriginal result\n '
    assert server._publication_text(original, [invalid]) == original
    result = server._publication_text(original, [invalid, 2, 3])
    suffix = result[len(original):]
    assert json.loads(suffix.splitlines()[1]) == {
        'notices': [{'code': 'journal_surrogates_replaced', 'count': 5}]}
