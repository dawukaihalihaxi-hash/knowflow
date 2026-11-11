"""Offline end-to-end configuration contract: save, reload, selected runtime, adapters."""
import copy,io,json,os,unittest
from unittest.mock import patch
import test_web_app
from assistant_catalog import AssistantCatalog
from answer_service import AnswerService
from rag_pipeline import Pipeline

class ConfigurationEffects(unittest.TestCase):
 def setUp(self):
  test_web_app.WebTests.setUp(self)
  self.original_accent=json.loads(self.path.read_text())['web']['accent']
 csrf=test_web_app.WebTests.csrf
 post=test_web_app.WebTests.post
 def save(self,changes=None,prompt=None):
  d=self.client.get('/api/settings').get_json()
  for path,value in (changes or {}).items():
   obj=d['config'];keys=path.split('.')
   for k in keys[:-1]:obj=obj[k]
   obj[keys[-1]]=value
  if prompt:d['prompt']=prompt
  r=self.post('/api/settings',d);self.assertEqual(r.status_code,200,r.get_json());return r.get_json()
 def test_every_exposed_field_persists_and_runtime_reads_it(self):
  changes={
   'web.assistant_name':'核验助手','web.company_name':'核验企业','web.description':'核验介绍',
   'web.examples':['核验问题'],'web.welcome_title':'核验欢迎','web.welcome_subtitle':'核验说明',
   'web.accent':'#8c3177','web.theme':'night','web.request_limit_per_minute':17,
   'answer.model':'audit-answer','answer.base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1',
   'answer.parameters.temperature':.31,'answer.parameters.max_tokens':2048,'answer.parameters.enable_thinking':True,
   'answer.parameters.thinking_budget':512,'answer.timeout_seconds':75,'answer.max_question_characters':300,
   'answer.max_claims':4,'answer.minimum_quote_characters':6,
   'answer.messages.insufficient':'核验不足','answer.messages.conflict':'核验冲突','answer.messages.invalid_response':'核验失败',
   'embedding.batch_size':7,'embedding.timeout_seconds':65,'database.batch_size':11,'database.timeout_seconds':55,
   'retrieval.vector_top_k':9,'retrieval.keyword_top_k':8,'retrieval.rerank_candidate_limit':7,
   'retrieval.context_top_k':3,'retrieval.minimum_cosine_similarity':.12,'retrieval.max_context_characters':3000,
   'retrieval.rrf_constant':40,'retrieval.source_groups':[]}
  out=self.save(changes,prompt='这是离线配置核验模板。'*10);self.assertFalse(out['pending_index'])
  catalog=AssistantCatalog(self.path);record=catalog.get();runtime=Pipeline(catalog.path(record));svc=AnswerService(runtime)
  for path,value in changes.items():
   with self.subTest(path=path):
    for config in [self.client.get('/api/settings').get_json()['config'],runtime.cfg]:
     obj=config
     for k in path.split('.'):obj=obj[k]
     self.assertEqual(obj,value)
  self.assertIn('这是离线配置核验模板。'*10,svc.prompt)
  self.assertNotEqual(Pipeline(catalog.path(record,active=True)).cfg['web']['assistant_name'],'核验助手')
  public=self.client.get('/api/assistants/'+record['id']).get_json()
  self.assertEqual(public['brand']['assistant_name'],'出行通')
  self.assertIn(self.original_accent,self.client.get('/a/'+record['id']+'/theme.css').text)
 def test_saved_model_prompt_and_limits_reach_adapter(self):
  out=self.save({'answer.model':'audit-model','answer.timeout_seconds':73,'answer.parameters.temperature':.42,
   'answer.parameters.thinking_budget':512,'answer.max_question_characters':20,'answer.messages.insufficient':'没有匹配资料'})
  p=Pipeline(self.path);svc=AnswerService(p);row=p.rows[0]
  with patch.object(p,'search',return_value={'index_id':p.index_id,'evidence':[]}):self.assertEqual(svc.answer('测试')['answer'],'没有匹配资料')
  with patch.object(p,'search',return_value={'index_id':p.index_id,'evidence':[{'chunk_id':row['chunk_id']}]}),patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake'}),patch('answer_service.request',return_value={'choices':[{'finish_reason':'stop','message':{'content':'{"status":"insufficient","claims":[]}'}}]}) as call:
   svc.answer('测试');args=call.call_args.args
   self.assertEqual(args[2]['model'],'audit-model');self.assertEqual(args[2]['temperature'],.42);self.assertEqual(args[2]['thinking_budget'],512)
   self.assertIn(out['prompt'],args[2]['messages'][0]['content']);self.assertEqual(args[4],73)
  with self.assertRaises(Exception):svc.answer('字'*21)
 def test_retrieval_configuration_reaches_database_and_caps_context(self):
  self.save({'retrieval.vector_top_k':5,'retrieval.keyword_top_k':4,'retrieval.context_top_k':1,
    'retrieval.rerank_candidate_limit':2,'retrieval.max_context_characters':3000,'retrieval.minimum_cosine_similarity':.23,'retrieval.source_groups':[]})
  p=Pipeline(self.path)
  with patch.object(p,'embed_texts',return_value=[[1]*1024]),patch.object(p,'db',return_value=[]) as db:
   result=p.search('儿童购票');payload=db.call_args.args[2]
   self.assertEqual(payload['p_limit'],5);self.assertEqual(payload['p_threshold'],.23)
   self.assertLessEqual(len(result['evidence']),1);self.assertLessEqual(result['context_characters'],3000)
 def test_logo_upload_revision_and_separate_color_save(self):
  aid=AssistantCatalog(self.path).get()['id'];d=self.client.get('/api/settings').get_json();old=d['revision']
  raw=b'\x89PNG\r\n\x1a\n'+b'x'*(1024*1024+1)
  r=self.client.post('/api/admin/assistants/'+aid+'/logo',data={'revision':old,'file':(io.BytesIO(raw),'logo.png')},headers={'X-CSRF-Token':self.csrf()})
  self.assertEqual(r.status_code,200,r.get_json());uploaded=r.get_json();self.assertNotEqual(uploaded['revision'],old)
  response=self.client.get(uploaded['url']);self.assertEqual(response.data,raw);response.close()
  d['revision']=uploaded['revision'];d['config']['web']['logo_asset']=uploaded['url'];d['config']['web']['logo_version']=uploaded['logo_version'];d['config']['web']['accent']='#225599'
  self.assertEqual(self.post('/api/settings',d).status_code,200)
  public=self.client.get('/api/assistants/'+aid).get_json();self.assertEqual(public['brand']['accent'],self.original_accent)
  self.assertNotEqual(public['brand']['logo_asset'],uploaded['url'])
  bad=self.client.post('/api/admin/assistants/'+aid+'/logo',data={'revision':old,'file':(io.BytesIO(raw),'logo.png')},headers={'X-CSRF-Token':self.csrf()});self.assertEqual(bad.status_code,400)
 def test_csrf_refresh_and_retry_preserves_cross_origin_protection(self):
  self.assertEqual(self.client.post('/api/settings',json={},headers={'X-CSRF-Token':'stale'}).get_json()['code'],'csrf_expired')
  token=self.client.get('/api/session').get_json()['csrf'];d=self.client.get('/api/settings').get_json()
  self.assertEqual(self.client.post('/api/settings',json=d,headers={'X-CSRF-Token':token}).status_code,200)
  self.assertEqual(self.client.post('/api/settings',json=d,headers={'X-CSRF-Token':token,'Origin':'https://evil.example'}).status_code,403)
 def test_pending_index_is_reported_and_old_runtime_retained(self):
  old=Pipeline(self.path).cfg['embedding']['model']
  d=self.save({'embedding.model':'audit-embedding','embedding.dimensions':512})
  self.assertTrue(d['pending_index']);catalog=AssistantCatalog(self.path)
  self.assertEqual(Pipeline(catalog.path(catalog.get(),active=True)).cfg['embedding']['model'],old)
  self.assertEqual(Pipeline(self.path).cfg['embedding']['dimensions'],512)
 def test_rate_limit_keeps_published_value_until_release(self):
  self.save({'web.request_limit_per_minute':1})
  with patch('web_app.AnswerService.answer',return_value={'status':'insufficient'}):
   self.assertEqual(self.post('/api/ask',{'question':'测试'}).status_code,200)
   self.assertEqual(self.post('/api/ask',{'question':'测试'}).status_code,200)
