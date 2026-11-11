import io,json,os,shutil,sys,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from web_app import create_app
from assistant_catalog import AssistantCatalog
from document_store import DocumentStore,extract
from rag_pipeline import Pipeline,PipelineError

class LifecycleTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  for folder in ('config','artifacts','knowledge','prompts','templates','static'):shutil.copytree(ROOT/folder,self.root/folder)
  self.path=self.root/'config/rag.railway.json';self.env=patch.dict(os.environ,{'APP_ENV':'local','APP_ALLOW_ADVANCED_SETTINGS':'1'},clear=True);self.env.start();self.addCleanup(self.env.stop)
  self.app=create_app(self.path,testing=True);self.client=self.app.test_client();self.client.get('/admin')
  self.catalog=AssistantCatalog(self.path);self.default=self.catalog.get()['id']
 def csrf(self):
  with self.client.session_transaction() as s:return s['csrf']
 def post(self,url,data):return self.client.post(url,json=data,headers={'X-CSRF-Token':self.csrf()})
 def create(self,name='测试助手',profile=None):
  r=self.post('/api/admin/assistants',{'name':name,'company':'测试企业','accent':'#387ac4','theme':'night','profile':profile});self.assertEqual(r.status_code,201,r.get_json());return r.get_json()
 def upload(self,aid,text='报销政策：员工需要提供真实有效的发票。\n\n提交期限为每月五号之前。'):
  r=self.client.post('/api/admin/assistants/'+aid+'/documents',data={'file':(io.BytesIO(text.encode()),'制度.txt')},headers={'X-CSRF-Token':self.csrf()});self.assertEqual(r.status_code,201,r.get_json());return r.get_json()
 def test_admin_and_chat_are_distinct_and_default_brand(self):
  self.assertIn('知序 KnowFlow',self.client.get('/admin').get_data(as_text=True))
  out=self.client.get('/api/assistants/'+self.default).get_json();self.assertEqual(out['brand']['assistant_name'],'出行通')
  page=self.client.get('/a/'+self.default).get_data(as_text=True)
  self.assertIn('assistant.css',page);self.assertNotIn('field-answer',page);self.assertNotIn('工作空间配置',page)
 def test_name_only_creates_draft_without_seed_knowledge_or_tenant_reuse(self):
  a=self.create();record=self.catalog.get(a['id']);c=json.loads(self.catalog.path(record).read_text());p=Pipeline(self.catalog.path(record),allow_empty=True)
  self.assertEqual(p.rows,[]);self.assertFalse(a['enabled']);self.assertFalse(a['ready']);self.assertNotEqual(c['tenant_id'],json.loads(self.path.read_text())['tenant_id'])
  self.assertEqual(self.post('/api/assistants/'+a['id']+'/ask',{'question':'test'}).status_code,400)
  self.assertEqual(self.post('/api/admin/assistants/'+a['id']+'/activate',{}).status_code,400)
 def test_existing_profile_requires_build_before_activation(self):
  profile=json.loads((self.root/'config/knowledge-profiles.json').read_text())[0]['id'];a=self.create(profile=profile)
  self.assertEqual(a['chunks'],202);self.assertFalse(a['ready'])
 def test_upload_review_and_chunk_provenance(self):
  a=self.create();d=self.upload(a['id']);self.assertEqual(d['status'],'approved');store=DocumentStore(self.catalog,a['id'])
  prepared,_=store.prepare_version();self.assertEqual(len(Pipeline(prepared).rows),2)
  with self.assertRaises(PipelineError):store.approve(d['id'],{'confirmed':True,'text_sha256':'wrong'})
  store.approve(d['id'],{'confirmed':True,'text_sha256':d['text_sha256']})
  path,ids=store.prepare_version();p=Pipeline(path)
  self.assertEqual(len(p.rows),2);self.assertTrue(all(x['document_id']=='upload-'+d['id'] for x in p.rows))
  self.assertEqual('\n\n'.join(x['body'] for x in p.rows),store.preview(d['id'])['text'])
  self.assertFalse(self.catalog.get(a['id'])['active_path'])
 def test_cross_assistant_document_access_blocked(self):
  one=self.create('甲助手');two=self.create('乙助手');doc=self.upload(one['id'])
  self.assertEqual(self.client.get('/api/admin/assistants/'+two['id']+'/documents/'+doc['id']).status_code,400)
  self.assertEqual(self.client.get('/a/'+two['id']+'/sources/'+doc['id']).status_code,404)
 def test_upload_markup_is_data_and_not_html_page(self):
  a=self.create();d=self.upload(a['id'],'<script>alert(1)</script> 这是上传的说明资料。')
  preview=DocumentStore(self.catalog,a['id']).preview(d['id']);self.assertIn('<script>',preview['text'])
  self.assertEqual(d['status'],'approved')
 def test_upload_limits_types_docx_and_invalid_encoding(self):
  with self.assertRaises(PipelineError):extract(b'<script>evil</script>','.html')
  with self.assertRaises(PipelineError):extract(b'\xff\xfe','.txt')
  with self.assertRaises(PipelineError):extract(b'a'*(10*1024*1024+1),'.txt')
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>这是一个可以正确提取的企业制度文档。</w:t></w:r></w:p></w:body></w:document>')
  self.assertIn('企业制度',extract(buf.getvalue(),'.docx'))
 def test_failed_build_preserves_old_active_index(self):
  record=self.catalog.get();old=record['active_path'];d=self.upload(record['id']);docs=DocumentStore(self.catalog,record['id']);docs.approve(d['id'],{'confirmed':True,'text_sha256':d['text_sha256']})
  with patch('web_app.Pipeline.embed',side_effect=PipelineError('mock unavailable')):
   r=self.post('/api/build?assistant='+record['id'],{'include_uploads':True});self.assertEqual(r.status_code,202)
   for _ in range(100):
    state=self.catalog.get(record['id'])['job']['state']
    if state!='running':break
    time.sleep(.01)
  self.assertEqual(self.catalog.get(record['id'])['active_path'],old);self.assertEqual(self.catalog.get(record['id'])['job']['state'],'failed')
 def test_successful_build_then_explicit_activation(self):
  a=self.create();d=self.upload(a['id']);docs=DocumentStore(self.catalog,a['id']);docs.approve(d['id'],{'confirmed':True,'text_sha256':d['text_sha256']})
  with patch('web_app.Pipeline.embed',return_value={}),patch('web_app.Pipeline.upload',return_value={'state':'ready'}):
   self.assertEqual(self.post('/api/build?assistant='+a['id'],{'include_uploads':True}).status_code,202)
   for _ in range(100):
    if self.catalog.get(a['id'])['job']['state']!='running':break
    time.sleep(.01)
  self.assertFalse(self.catalog.get(a['id'])['enabled'])
  self.client.get('/api/admin/assistants/'+a['id']+'/preview')
  with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake','SUPABASE_URL':'https://fake.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_fake'}):
   self.assertEqual(self.post('/api/admin/assistants/'+a['id']+'/activate',{}).status_code,200)
  self.assertEqual(docs.get(d['id'])['status'],'approved')
  self.assertEqual(self.client.get('/a/'+a['id']+'/sources/'+d['id']).mimetype,'text/plain')
 def test_theme_and_logo_validation(self):
  a=self.create();css=self.client.get('/a/'+a['id']+'/theme.css');self.assertIn('#387ac4',css.get_data(as_text=True))
  r=self.client.post('/api/admin/assistants/'+a['id']+'/logo',data={'file':(io.BytesIO(b'<svg/>'),'logo.svg')},headers={'X-CSRF-Token':self.csrf()});self.assertEqual(r.status_code,400)
 def test_empty_enterprise_starts_with_setup(self):
  self.catalog.state.unlink()
  with patch.dict(os.environ,{'APP_START_EMPTY':'1'}):
   app=create_app(self.path,testing=True);c=app.test_client();r=c.get('/');self.assertEqual(r.location,'/setup')
 def test_incremental_import_reuses_matching_vectors_in_new_version(self):
  record=self.catalog.get();base=Pipeline(self.catalog.path(record));row=base.rows[0]
  from rag_pipeline import sha
  base.save_cache({'index_id':base.index_id,'profile':base.profile,'vectors':{row['chunk_id']:{'input_sha256':sha(row['embedding_text'].encode()),'vector':[1.0]+[0.0]*1023}}})
  doc=self.upload(record['id']);docs=DocumentStore(self.catalog,record['id']);docs.approve(doc['id'],{'confirmed':True,'text_sha256':doc['text_sha256']})
  path,_=docs.prepare_version();candidate=Pipeline(path);cache=candidate.cache()
  self.assertNotEqual(candidate.index_id,base.index_id);self.assertEqual(cache['index_id'],candidate.index_id)
  self.assertIn(row['chunk_id'],cache['vectors'])
  with patch.object(candidate,'embed_texts',side_effect=lambda texts:[[1.0]+[0.0]*1023 for _ in texts]):
   result=candidate.embed()
  self.assertEqual(result['new_vectors'],len(candidate.rows)-1)
 def test_pending_model_settings_keep_active_version(self):
  record=self.catalog.get();old_index=record['active_index']
  d=self.client.get('/api/settings?assistant='+record['id']).get_json();d['config']['embedding']['model']='test-other-model'
  result=self.post('/api/settings?assistant='+record['id'],d);self.assertEqual(result.status_code,200,result.get_json())
  current=self.catalog.get(record['id']);self.assertEqual(Pipeline(self.catalog.path(current,active=True)).index_id,old_index)
  self.assertNotEqual(Pipeline(self.catalog.path(current)).index_id,old_index)
 def test_regular_user_cannot_create_or_preview_draft(self):
  a=self.create()
  with patch.dict(os.environ,{'APP_ENV':'production','APP_USER_PASSWORD':'user','APP_ADMIN_PASSWORD':'admin','APP_SESSION_SECRET':'test-secret'}):
   app=create_app(self.path,testing=True);c=app.test_client();c.get('/login',base_url='https://example.com')
   with c.session_transaction(base_url='https://example.com') as session:token=session['csrf']
   c.post('/login',json={'csrf':token,'password':'user'},base_url='https://example.com')
   self.assertEqual(c.get('/api/admin/assistants',base_url='https://example.com').status_code,403)
   self.assertEqual(c.get('/a/'+a['id'],base_url='https://example.com').status_code,403)

if __name__=='__main__':unittest.main()
