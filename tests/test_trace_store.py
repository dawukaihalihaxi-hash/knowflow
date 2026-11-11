import json,os,unittest
from unittest.mock import patch
import test_web_app
from assistant_catalog import AssistantCatalog
from trace_store import save_trace,load_trace,list_traces
from rag_pipeline import PipelineError
class TraceTests(unittest.TestCase):
 setUp=test_web_app.WebTests.setUp
 csrf=test_web_app.WebTests.csrf
 post=test_web_app.WebTests.post
 def test_trace_is_private_bounded_and_assistant_scoped(self):
  aid=AssistantCatalog(self.path).get()['id']
  first=save_trace(self.root,aid,{'question':'one'},1);second=save_trace(self.root,aid,{'question':'two'},1)
  self.assertEqual(len(list_traces(self.root,aid)),1)
  with self.assertRaises(PipelineError):load_trace(self.root,aid,first)
  with self.assertRaises(PipelineError):load_trace(self.root,'another',second)
  with self.assertRaises(PipelineError):load_trace(self.root,aid,'../secret')
  path=self.root/'local-data/answer-traces'/aid/(second+'.json');self.assertEqual(path.stat().st_mode&0o777,0o600)
 def test_real_web_route_records_failed_validation_and_exact_input_without_keys(self):
  c=json.loads(self.path.read_text());c['retrieval']['trace_enabled']=True;self.path.write_text(json.dumps(c))
  d=self.client.get('/api/settings').get_json();d['config']['retrieval']['trace_enabled']=True
  self.assertEqual(self.post('/api/settings',d).status_code,200)
  aid=AssistantCatalog(self.path).get()['id']
  p=__import__('rag_pipeline').Pipeline(self.path);row=p.rows[0]
  response={'choices':[{'finish_reason':'stop','message':{'content':'{"status":"answered","claims":[]}', 'reasoning_content':'do not record reasoning'}}]}
  with patch('web_app.Pipeline.search',return_value={'index_id':p.index_id,'evidence':[{'chunk_id':row['chunk_id']}]}),patch('answer_service.request',return_value=response),patch.dict(os.environ,{'DASHSCOPE_API_KEY':'test_private_key'}):
   result=self.post('/api/assistants/'+aid+'/ask',{'question':'问题 <script>test</script>'}).get_json()
  tid=result['trace_id'];trace=load_trace(self.root,aid,tid)
  self.assertEqual(trace['result']['status'],'invalid_response');self.assertIn('model_input',trace)
  self.assertNotIn('test_private_key',json.dumps(trace));self.assertNotIn('do not record reasoning',json.dumps(trace))
  self.assertEqual(self.client.get('/api/admin/assistants/'+aid+'/traces/'+tid).status_code,200)
  page=self.client.get('/admin/assistants/'+aid+'/traces?trace='+tid).get_data(as_text=True)
  self.assertIn('最终知识上下文',page);self.assertNotIn('<script>test</script>',page)
 def test_production_regular_user_cannot_read_trace_page_or_api(self):
  aid=AssistantCatalog(self.path).get()['id'];tid=save_trace(self.root,aid,{'question':'private'})
  from web_app import create_app
  with patch.dict(os.environ,{'APP_ENV':'production','APP_ADMIN_PASSWORD':'admin','APP_USER_PASSWORD':'user','APP_SESSION_SECRET':'test'},clear=True):
   app=create_app(self.path,testing=True);client=app.test_client();base='https://example.com';client.get('/login',base_url=base)
   with client.session_transaction(base_url=base) as session:token=session['csrf']
   client.post('/login',base_url=base,json={'password':'user','csrf':token})
   for url in ['/admin/assistants/'+aid+'/traces','/api/admin/assistants/'+aid+'/traces/'+tid]:self.assertEqual(client.get(url,base_url=base).status_code,403)
