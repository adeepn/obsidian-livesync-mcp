import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import pytest
from io import BytesIO
from urllib.parse import urlsplit,unquote
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ReadTimeoutError
from object_storage import Boto3ObjectStorage
from tests.test_object_storage import make_cfg
from tests.test_journal_security import backend

@pytest.mark.parametrize('mode', ['identical', 'absent', 'different', 'unreadable'])
async def test_lost_conditional_response_one_publication(tmp_path,monkeypatch,mode):
 peer,storage=await backend(tmp_path)
 sdk=Boto3ObjectStorage(make_cfg(max_attempts=4,prefix=''))
 calls=[];accepted=[]
 class Raw(BytesIO):
  def stream(self,amt=None,decode_content=False):yield self.read()
 def send(request):
  key=unquote(urlsplit(request.url).path.rsplit('/',1)[-1]);calls.append(key)
  if len(calls) > 1:
   return AWSResponse(request.url,412,{},Raw(b'<Error><Code>PreconditionFailed</Code></Error>'))
  body=request.body.read() if hasattr(request.body,'read') else request.body
  storage._objects[key]=body;accepted.append(key)
  if len(accepted)==1:
   if mode == 'absent': del storage._objects[key]
   if mode == 'different': storage._objects[key] = b'different'
   raise ReadTimeoutError(endpoint_url='http://synthetic.invalid')
  return AWSResponse(request.url,200,{},Raw(b''))
 monkeypatch.setattr(sdk._client._endpoint.http_session,'send',send)
 monkeypatch.setattr('botocore.endpoint.time.sleep',lambda _:None)
 original_get = storage.get
 def readback(key):
  if mode == 'unreadable':
   from storage_base import StorageUnavailable
   raise StorageUnavailable()
  return original_get(key)
 sdk.get = readback
 storage.put_if_absent=sdk.put_if_absent
 try:
  if mode != 'identical':
   from storage_base import OutcomeUnknown
   with pytest.raises(OutcomeUnknown):
    await peer.write_note('lost.md','synthetic payload',create=True)
   assert len(accepted) == 1
   assert len(peer._store.outbound_journals()) == 1
   assert peer._store.outbound_journals()[0][2] == 0
   assert peer._store.last_sent_key == ''
   return
  await peer.write_note('lost.md','synthetic payload',create=True)
  assert (await peer.read_note('lost.md'))['text']=='synthetic payload'
  print('accepted_objects',accepted,'wire_calls',calls,'identical_payload',len({storage._objects[k] for k in accepted})==1)
  assert peer._store.outbound_journals()[0][2] == 1
  assert peer._store.last_sent_key == accepted[0]
  assert len(accepted)==1,'one logical publication created two journal objects'
 finally:await peer.close()
