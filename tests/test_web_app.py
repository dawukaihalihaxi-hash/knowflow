import copy,json,os,re,shutil,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from web_app import create_app
from rag_pipeline import Pipeline,PipelineError
from settings_store import SettingsStore

class WebTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for folder in ('config','artifacts','knowledge','prompts','templates','static'):
   shutil.copytree(ROOT/folder,self.root/folder)
  self.path=self.root/'config/rag.railway.json'
  self.env=patch.dict(os.environ,{'APP_ENV':'local','APP_ALLOW_ADVANCED_SETTINGS':'1'},clear=True);self.env.start();self.addCleanup(self.env.stop)
  self.app=create_app(self.path,testing=True);self.client=self.app.test_client();self.client.get('/')
 def csrf(self):
  with self.client.session_transaction() as s:return s['csrf']
 def post(self,url,data,**kw):return self.client.post(url,json=data,headers={'X-CSRF-Token':self.csrf()},**kw)
 def test_pages_and_no_secret_export(self):
  self.assertEqual(self.client.get('/').status_code,302)
  for path in ('/admin','/settings'):self.assertEqual(self.client.get(path).status_code,200)
  os.environ['DASHSCOPE_API_KEY']='fake_private_secret'
  out=self.client.get('/api/settings').get_json()
  self.assertTrue(out['credentials']['DASHSCOPE_API_KEY']);self.assertNotIn('fake_private_secret',json.dumps(out))
 def test_csrf_cross_origin_and_remote_access(self):
  self.assertEqual(self.client.post('/api/ask',json={'question':'test'}).status_code,403)
  self.assertEqual(self.client.post('/api/ask',json={'question':'test'},headers={'X-CSRF-Token':self.csrf(),'Origin':'https://evil.example'}).status_code,403)
  self.assertEqual(self.client.get('/api/settings',base_url='http://evil.example',environ_overrides={'REMOTE_ADDR':'192.0.2.1'}).status_code,403)
 def test_save_brand_prompt_and_reload(self):
  data=self.client.get('/api/settings').get_json();data['config']['web']['company_name']='测试企业'
  data['prompt']+='\n请保持简明。'
  result=self.post('/api/settings',data);self.assertEqual(result.status_code,200,result.get_json())
  out=result.get_json();self.assertIn('versions/',out['config']['answer']['prompt_path'])
  self.assertEqual(self.client.get('/api/status').get_json()['web']['company_name'],'测试企业')
  self.assertEqual(self.post('/api/settings',data).status_code,400)
 def test_settings_reject_unknown_hosts_path_tenant_and_bad_topk(self):
  for mutate in (lambda c:c['answer'].update(base_url='https://evil.example/v1'),lambda c:c.update(tenant_id='other'),lambda c:c['retrieval'].update(vector_top_k=999),lambda c:c['answer'].update(prompt_path='../../secret')):
   d=self.client.get('/api/settings').get_json();mutate(d['config']);self.assertEqual(self.post('/api/settings',d).status_code,400)
 def test_model_change_creates_new_cache_path(self):
  d=self.client.get('/api/settings').get_json();old=d['index_id'];d['config']['embedding']['model']='test-model'
  result=self.post('/api/settings',d);self.assertEqual(result.status_code,200,result.get_json())
  out=result.get_json();self.assertNotEqual(out['index_id'],old);self.assertIn(out['index_id'],out['config']['vector_cache_path'])
 def test_credentials_write_no_echo_preserve_on_blank(self):
  out=self.post('/api/credentials',{'DASHSCOPE_API_KEY':'private_fake_value'});self.assertEqual(out.status_code,200)
  self.assertNotIn('private_fake_value',out.get_data(as_text=True));self.assertEqual((self.root/'.env').stat().st_mode&0o777,0o600)
  self.post('/api/credentials',{'DASHSCOPE_API_KEY':''});self.assertEqual(os.environ['DASHSCOPE_API_KEY'],'private_fake_value')
 def test_ask_rejects_tenant_override_and_uses_service(self):
  self.assertEqual(self.post('/api/ask',{'question':'hi','tenant_id':'other'}).status_code,400)
  with patch('web_app.AnswerService.answer',return_value={'status':'insufficient','answer':'资料不足','claims':[],'sources':[]}) as call:
   result=self.post('/api/ask',{'question':'hi'});self.assertEqual(result.status_code,200);call.assert_called_once_with('hi')
 def test_production_requires_auth_and_user_cannot_configure(self):
  with patch.dict(os.environ,{'APP_ENV':'production'},clear=True):
   with self.assertRaises(RuntimeError):create_app(self.path)
  with patch.dict(os.environ,{'APP_ENV':'production','APP_ADMIN_PASSWORD':'admin-pass','APP_USER_PASSWORD':'user-pass','APP_SESSION_SECRET':'test-session-secret'},clear=True):
   app=create_app(self.path,testing=True);c=app.test_client();self.assertEqual(c.get('/api/settings',base_url='https://example.com').status_code,401)
   c.get('/login',base_url='https://example.com')
   with c.session_transaction(base_url='https://example.com') as s:token=s['csrf']
   self.assertEqual(c.post('/login',base_url='https://example.com',json={'password':'user-pass','csrf':token}).status_code,200)
   self.assertEqual(c.get('/api/settings',base_url='https://example.com').status_code,403)
 def test_student_sources_preserved_without_reranker(self):
  p=Pipeline(self.path)
  with patch.object(p,'embed_texts',return_value=[[1]*1024]),patch.object(p,'db',return_value=[]):result=p.search('2023与2025学生新生购票核验有何变化？')
  ids={e['document_id'] for e in result['evidence']};self.assertIn('moe-cr-2023-4',ids);self.assertIn('cr-2025-student',ids)

 def test_thinking_budget_is_configurable_and_bounded(self):
  data=self.client.get('/api/settings').get_json();data['config']['answer']['parameters']['thinking_budget']=1024
  result=self.post('/api/settings',data);self.assertEqual(result.status_code,200,result.get_json())
  data=result.get_json();data['config']['answer']['parameters']['thinking_budget']=32769
  self.assertEqual(self.post('/api/settings',data).status_code,400)


 def test_unified_configuration_flow(self):
  page=self.client.get('/setup').get_data(as_text=True)
  for tab in ('identity','knowledge','models','retrieval','answer'):
   self.assertIn('data-tab="'+tab+'"',page)
  new=self.post('/api/admin/assistants',{'name':'测试助手','company':'测试企业','unified_flow':True}).get_json()
  aid=new['id'];url='/api/admin/assistants/'+aid+'/configuration'
  self.assertFalse(self.client.get(url).get_json()['complete'])
  self.assertEqual(self.post(url,{'step':5}).status_code,400)
  self.assertEqual(self.post(url,{'step':2}).status_code,400)
  self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,400)
  from document_store import DocumentStore
  from assistant_catalog import AssistantCatalog
  docs=DocumentStore(AssistantCatalog(self.path),aid)
  uploaded=docs.upload('guide.txt','这是企业知识资料，供用户查询业务办理要求。'.encode())
  # Approve through the same public API and saved extraction hash as the UI.
  detail=self.client.get('/api/admin/assistants/'+aid+'/documents/'+uploaded['id']).get_json()
  response=self.post('/api/admin/assistants/'+aid+'/documents/'+uploaded['id'],{'confirmed':True,'text_sha256':detail['text_sha256'],'title':detail['title'],'source_url':'','issued_date':''})
  self.assertEqual(response.status_code,200,response.get_json())
  self.assertEqual(self.post(url,{'step':2}).status_code,200)
  self.assertEqual(self.post(url,{'step':3}).status_code,400)
  with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'test','SUPABASE_URL':'https://example.supabase.co','SUPABASE_SECRET_KEY':'test'}):
   for step in (3,4,5):
    response=self.post(url,{'step':step});self.assertEqual(response.status_code,200,response.get_json())
  self.assertTrue(self.client.get(url).get_json()['complete'])

 def test_delete_restore_and_incomplete_preview(self):
  a=self.post('/api/admin/assistants',{'name':'待配置','company':'企业','unified_flow':True}).get_json();aid=a['id']
  self.assertEqual(self.client.get('/a/'+aid).status_code,403)
  self.assertEqual(self.client.get('/api/assistants/'+aid).status_code,403)
  self.assertEqual(self.post('/api/assistants/'+aid+'/ask',{'question':'test'}).status_code,400)
  self.assertEqual(self.post('/api/admin/assistants/'+aid+'/delete',{}).status_code,200)
  self.assertEqual(self.client.get('/settings?assistant='+aid).status_code,400)
  self.assertEqual(self.client.get('/a/'+aid).status_code,400)
  deleted=self.client.get('/api/admin/deleted-assistants').get_json()['assistants'];self.assertIn(aid,[x['id'] for x in deleted])
  self.assertEqual(self.post('/api/admin/assistants/'+aid+'/restore',{}).status_code,200)
  self.assertEqual(self.client.get('/a/'+aid).status_code,403)
  # The original enabled assistant can be removed too, without deleting shared files.
  self.assertEqual(self.post('/api/admin/assistants/chuxingtong/delete',{}).status_code,200)
  self.assertEqual(self.client.get('/a/chuxingtong').status_code,400)
  self.assertEqual(self.post('/api/admin/assistants/chuxingtong/restore',{}).status_code,200)
  self.assertFalse(self.client.get('/api/admin/assistants').get_json()['assistants'][0]['enabled'])

if __name__=='__main__':unittest.main()
