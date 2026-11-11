"""Network error handling must name the failing stage without exposing credentials."""
import io,socket,ssl,sys,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from rag_pipeline import request,PipelineError,ServiceConnectionError
class ServiceConnectionTests(unittest.TestCase):
 def test_each_stage_is_named_and_request_is_not_retried(self):
  for path,name in [('/embeddings','向量模型'),('/chat/completions','回答模型'),('/rest/v1/rpc/match_knowledge_chunks','知识数据库')]:
   with self.subTest(stage=name),patch('rag_pipeline.urllib.request.urlopen',side_effect=urllib.error.URLError(ConnectionResetError('private detail'))) as call,self.assertLogs('rag_pipeline',level='WARNING') as logs:
    with self.assertRaisesRegex(PipelineError,name):request('https://example.com'+path,'POST',{'private':'question'},{'Authorization':'secret'})
    self.assertEqual(call.call_count,2 if name=='知识数据库' else 1);self.assertNotIn('private',str(logs.output));self.assertNotIn('secret',str(logs.output))
 def test_error_categories_are_actionable(self):
  for reason,word in [(TimeoutError(),'超时'),(socket.gaierror(),'域名解析'),(ssl.SSLError(),'安全连接'),(ssl.SSLCertVerificationError(),'证书')]:
   with self.subTest(reason=type(reason).__name__),patch('rag_pipeline.urllib.request.urlopen',side_effect=urllib.error.URLError(reason)),self.assertLogs('rag_pipeline',level='WARNING'):
    with self.assertRaisesRegex(PipelineError,word):request('https://example.com/embeddings')
 def test_billing_error_stays_distinct_from_connection_failure(self):
  error=urllib.error.HTTPError('https://example.com',400,'bad',{},io.BytesIO(b'{"error":{"code":"Arrearage"}}'))
  with patch('rag_pipeline.urllib.request.urlopen',side_effect=error):
   with self.assertRaisesRegex(PipelineError,'计费状态'):request('https://example.com/embeddings')
  error.close()

 def test_read_retry_recovers_without_replaying_writes_or_models(self):
  with patch('rag_pipeline._request_once',side_effect=[ServiceConnectionError('broken'),{'ok':True}]) as call,patch('rag_pipeline.time.sleep'):
   self.assertEqual(request('https://example.com/rest/v1/rpc/match_knowledge_chunks','POST'),{'ok':True});self.assertEqual(call.call_count,2)
  for path in ['/rest/v1/knowledge_chunks','/embeddings','/chat/completions','/rest/v1/rpc/finalize_knowledge_index']:
   with self.subTest(path=path),patch('rag_pipeline._request_once',side_effect=ServiceConnectionError('broken')) as call:
    with self.assertRaises(ServiceConnectionError):request('https://example.com'+path,'POST')
    call.assert_called_once()
