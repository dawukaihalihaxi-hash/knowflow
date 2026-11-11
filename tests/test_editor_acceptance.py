"""Requirement-focused offline repair checks; no outbound service calls."""
import copy,io,json,os,time,unittest
from unittest.mock import patch
import test_assistant_lifecycle as lifecycle
from assistant_catalog import read
from document_store import DocumentStore
from rag_pipeline import Pipeline,PipelineError
from answer_service import AnswerService

class EditorAcceptance(unittest.TestCase):
 setUp=lifecycle.LifecycleTests.setUp
 csrf=lifecycle.LifecycleTests.csrf
 post=lifecycle.LifecycleTests.post
 create=lifecycle.LifecycleTests.create
 upload=lifecycle.LifecycleTests.upload
 def payload(self,aid):
  d=self.client.get('/api/admin/assistants/'+aid+'/editor').json
  return {key:d[key] for key in ('revision','web','top_k','business')}|{'models':{k:d['models'][k] for k in ('answer','embedding')}}
 def approve(self,aid,d):return DocumentStore(self.catalog,aid).approve(d['id'],{'confirmed':True,'text_sha256':d['text_sha256']})
 def wait(self,aid):
  for _ in range(500):
   job=self.catalog.get(aid)['job']
   if job['state']!='running':return job
   time.sleep(.01)
  self.fail('job did not finish')
 def test_json_errors_delete_restore_and_method_contract(self):
  aid=self.create()['id'];base='/api/admin/assistants/'+aid
  for response in [self.client.get(base+'/delete'),self.post('/api/admin/missing',{}),self.client.post(base+'/delete',json={})]:
   self.assertIn(response.status_code,(403,404,405));self.assertTrue(response.is_json);self.assertTrue(response.json['error'])
  with patch('web_app.AssistantCatalog.remove',side_effect=OSError('disk unavailable')):
   response=self.post(base+'/delete',{});self.assertEqual(response.status_code,500);self.assertIn('incident',response.json)
  response=self.post(base+'/delete',{});self.assertTrue(response.json['ok']);self.assertTrue(response.json['message'])
  self.assertEqual(self.post(base+'/delete',{}).status_code,400)
  response=self.post(base+'/restore',{});self.assertTrue(response.json['ok']);self.assertFalse(self.catalog.get(aid)['enabled'])
 def test_editor_returns_only_form_fields_and_rejects_engineering_writes(self):
  aid=self.default;response=self.client.get('/api/admin/assistants/'+aid+'/editor');text=response.get_data(as_text=True)
  for secret in ('prompt_path','base_url','api_key_env','source_groups','query_rules','vector_cache_path'):self.assertNotIn(secret,text)
  d=self.payload(aid);d['models']['base_url']='https://example.com';self.assertEqual(self.post('/api/admin/assistants/'+aid+'/editor',d).status_code,400)
  d=self.payload(aid);d['top_k']['context_top_k']=101;self.assertEqual(self.post('/api/admin/assistants/'+aid+'/editor',d).status_code,400)
  d=self.payload(aid);d['top_k']['context_top_k']=2;self.assertEqual(self.post('/api/admin/assistants/'+aid+'/editor',d).status_code,200)
 def test_business_defaults_save_reload_and_actual_adapter_prompt(self):
  a=self.create()['id'];b=self.create()['id'];d=self.payload(a)
  self.assertTrue(all(d['business'].values()));d['business']['tone']='用三条短句回答，不使用缩写。'
  response=self.post('/api/admin/assistants/'+a+'/editor',d);self.assertEqual(response.status_code,200,response.json)
  self.assertEqual(self.payload(a)['business']['tone'],d['business']['tone']);self.assertNotEqual(self.payload(b)['business']['tone'],d['business']['tone'])
  p=Pipeline(self.catalog.path(self.catalog.get(a)),allow_empty=True);svc=AnswerService(p)
  self.assertIn(d['business']['tone'],svc.prompt);self.assertIn('平台基础规则',svc.prompt)
  self.assertTrue(list((self.catalog.path(self.catalog.get(a)).parent.parent/'local-data/settings-history').glob('*.json')))
 def test_model_selection_registered_only_and_published_unchanged(self):
  options=read(self.root/'config/model-options.json');options['answer'].append({'id':'offline-answer','label':'离线测试选项'})
  (self.root/'config/model-options.json').write_text(json.dumps(options))
  old=read(self.path)['answer']['model'];d=self.payload(self.default);d['models']['answer']='offline-answer'
  response=self.post('/api/admin/assistants/'+self.default+'/editor',d);self.assertEqual(response.status_code,200,response.json)
  self.assertEqual(self.payload(self.default)['models']['answer'],'offline-answer')
  self.assertEqual(read(self.catalog.path(self.catalog.get(),active=True))['answer']['model'],old)
  d=self.payload(self.default);d['models']['answer']='unregistered';self.assertEqual(self.post('/api/admin/assistants/'+self.default+'/editor',d).status_code,400)
 def test_inventory_details_action_visibility_and_duplicate_upload(self):
  docs=DocumentStore(self.catalog,self.default);rows=docs.listing();self.assertEqual(len(rows),8)
  self.assertEqual(sum(x['active_chunks'] for x in rows),202);self.assertEqual(sum(x['total_chunks'] for x in rows),234)
  for d in rows:
   detail=self.client.get('/api/admin/assistants/'+self.default+'/documents/'+d['id']).json
   for key in ('source_url','original_url','text','review_status','participating','active_chunks','total_chunks','active_version'):self.assertIn(key,detail)
   if d['status']=='quarantined':self.assertEqual(d['allowed_actions'],['delete'])
   else:self.assertIn('remove',d['allowed_actions']);self.assertIn('delete',d['allowed_actions'])
  doc=self.upload(self.default);again=self.upload(self.default);self.assertEqual(doc['id'],again['id']);self.assertTrue(again['duplicate'])
  row=next(x for x in docs.listing() if x['id']==doc['id']);self.assertEqual(row['allowed_actions'],['delete','replace']);self.assertEqual(row['build_state'],'pending');self.assertIsNone(row['built_chunks'])
  result=docs.change(doc['id'],'delete');self.assertTrue(result['ok']);self.assertIn('已删除',result['message']);self.assertEqual(next(x for x in docs.listing() if x['id']==doc['id'])['allowed_actions'],['restore'])
  docs.change(doc['id'],'restore');self.assertFalse(docs.get(doc['id'])['removed'])
 def test_real_local_stages_with_fake_service_and_cached_retry(self):
  aid=self.create()['id'];doc=self.upload(aid);self.approve(aid,doc)
  p=Pipeline(self.catalog.path(self.catalog.get(aid)),allow_empty=True);dims=p.profile['dimensions']
  def fake_db(p,path,method='GET',payload=None,prefer=None):
   if path=='rpc/register_knowledge_index':return 'building'
   if path=='rpc/finalize_knowledge_index':return {'state':'ready'}
   return []
  with patch.object(Pipeline,'embed_texts',side_effect=lambda texts:[[1.0]+[0.0]*(dims-1) for _ in texts]),patch.object(Pipeline,'db',new=fake_db):
   self.assertEqual(self.post('/api/build?assistant='+aid,{}).status_code,202);job=self.wait(aid)
  self.assertEqual(job['state'],'done',job);self.assertEqual(set(job['stages']),{'parsing','chunking','embedding','upload','verification'})
  m=job['metrics'];self.assertEqual(m['new_vectors'],2);self.assertEqual(m['reused_vectors'],0);self.assertEqual(m['written_rows'],2);self.assertEqual(m['confirmed_rows'],2);self.assertEqual(m['failed_rows'],0);self.assertTrue(m['index_ready'])
  refreshed=self.client.get('/api/build?assistant='+aid).json;self.assertEqual(refreshed['metrics'],m)
  self.assertFalse(self.catalog.get(aid)['active_path'])
  built=next(d for d in DocumentStore(self.catalog,aid).listing() if d['id']==doc['id'])
  self.assertEqual(built['build_state'],'built');self.assertEqual(built['built_chunks'],2);self.assertEqual(built['built_retrievable_chunks'],2);self.assertIsNone(built['active_version'])
  with patch.object(Pipeline,'embed_texts',side_effect=AssertionError('cached texts must not be embedded')),patch.object(Pipeline,'db',new=fake_db):
   self.assertEqual(self.post('/api/build?assistant='+aid,{}).status_code,202);retry=self.wait(aid)
  self.assertEqual(retry['state'],'done',retry);self.assertEqual(retry['metrics']['reused_vectors'],2);self.assertEqual(retry['metrics']['new_vectors'],0)
  self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,400)
  self.client.get('/api/admin/assistants/'+aid+'/preview')
  with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'offline','SUPABASE_URL':'https://fake.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_offline'}):
   self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,200)
  state=self.client.get('/api/admin/assistants/'+aid+'/editor').json
  self.assertEqual(state['lifecycle']['state'],'published');self.assertFalse(state['knowledge']['added']);self.assertTrue(state['history'])
  self.assertIn('已发布',state['job']['message'])
  self.assertEqual(self.post('/api/admin/assistants/'+aid+'/editor',self.payload(aid)).status_code,200)
  self.assertEqual(self.client.get('/api/admin/assistants/'+aid+'/editor').json['lifecycle']['state'],'published')

 def test_protocol_version_guard_and_cache_headers(self):
  health=self.client.get('/healthz');version=health.json['version'];self.assertEqual(health.headers['X-KnowFlow-Version'],version)
  page=self.client.get('/settings').get_data(as_text=True);self.assertIn('name="app-version" content="'+version+'"',page);self.assertIn('settings.js?v='+version,page)
  response=self.client.get('/api/admin/assistants',headers={'X-KnowFlow-Version':'old-page'});self.assertEqual(response.status_code,409);self.assertEqual(response.json['code'],'version_mismatch')
  self.assertEqual(self.client.get('/static/http.js').headers['Cache-Control'],'no-store')
 def test_disabled_document_not_used_as_context_or_overview(self):
  p=Pipeline(self.path);p.blocked_documents={x['document_id'] for x in p.rows}
  with patch.object(p,'embed_texts',return_value=[[1]+[0]*1023]),patch.object(p,'db',return_value=[{'chunk_id':p.rows[0]['chunk_id'],'similarity':1}]):
   for query in ('铁路旅客运输规程','限制携带物品目录'):
    result=p.search(query);self.assertEqual(result['evidence'],[])
 def test_replacement_publication_history_and_immutable_original(self):
  aid=self.create()['id'];old=self.upload(aid);docs=DocumentStore(self.catalog,aid);self.approve(aid,old)
  def fake_db(p,path,method='GET',payload=None,prefer=None):return {'state':'ready'} if path=='rpc/finalize_knowledge_index' else 'building' if path=='rpc/register_knowledge_index' else []
  def publish():
   with patch.object(Pipeline,'embed_texts',side_effect=lambda texts:[[1.0]+[0.0]*1023 for _ in texts]),patch.object(Pipeline,'db',new=fake_db):
    self.assertEqual(self.post('/api/build?assistant='+aid,{}).status_code,202);self.assertEqual(self.wait(aid)['state'],'done')
   self.client.get('/api/admin/assistants/'+aid+'/preview')
   with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'offline','SUPABASE_URL':'https://fake.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_offline'}):self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,200)
  publish();old_record=self.catalog.get(aid);old_path=self.catalog.path(old_record,active=True);old_content=docs.preview(old['id'])['text']
  new=self.upload(aid,'新版制度：每月十号前提交。\n\n超时由主管另行审批。');self.approve(aid,new)
  docs.change(old['id'],'replace',new['id']);plan=docs.plan();self.assertEqual(len(plan['replaced']),1)
  self.assertEqual(self.catalog.path(self.catalog.get(aid),active=True),old_path)
  publish();active=Pipeline(self.catalog.path(self.catalog.get(aid),active=True))
  self.assertTrue(all(x['document_id']=='upload-'+new['id'] for x in active.rows))
  self.assertEqual(docs.preview(old['id'])['text'],old_content);self.assertTrue(old_path.exists())
  self.assertEqual(len(self.catalog.get(aid)['publications']),2)
  state=self.client.get('/api/admin/assistants/'+aid+'/editor').json;self.assertEqual(state['lifecycle']['state'],'published');self.assertEqual(state['knowledge']['replaced'],[])
 def test_engineering_model_profile_change_cannot_reuse_incompatible_vectors(self):
  p=Pipeline(self.path);row=p.rows[0]
  from rag_pipeline import sha
  p.save_cache({'index_id':p.index_id,'profile':p.profile,'vectors':{row['chunk_id']:{'input_sha256':sha(row['embedding_text'].encode()),'vector':[1.0]+[0.0]*1023}}})
  d=self.client.get('/api/settings').json;d['config']['embedding']['model']='offline-other-model';d['config']['embedding']['dimensions']=512
  self.assertEqual(self.post('/api/settings',d).status_code,200)
  path,_=DocumentStore(self.catalog,self.default).prepare_version();candidate=Pipeline(path)
  self.assertNotEqual(candidate.index_id,p.index_id);self.assertEqual(candidate.cache()['vectors'],{})
 def test_draft_logo_can_be_previewed_by_admin_before_completion(self):
  aid=self.post('/api/admin/assistants',{'name':'Logo草稿','company':'验收','unified_flow':True}).json['id']
  revision=self.client.get('/api/admin/assistants/'+aid+'/editor').json['revision']
  raw=b'\x89PNG\r\n\x1a\n'+b'offline-test'
  response=self.client.post('/api/admin/assistants/'+aid+'/logo',data={'revision':revision,'file':(io.BytesIO(raw),'logo.png')},headers={'X-CSRF-Token':self.csrf()})
  self.assertEqual(response.status_code,200,response.json)
  image=self.client.get(response.json['url']);self.assertEqual(image.status_code,200);self.assertEqual(image.data,raw);image.close()
 def test_unpublished_candidate_sources_and_theme_are_admin_only(self):
  aid=self.create()['id'];d=self.upload(aid);self.approve(aid,d)
  values=self.payload(aid);values['web']['accent']='#226644';self.assertEqual(self.post('/api/admin/assistants/'+aid+'/editor',values).status_code,200)
  with patch('web_app.Pipeline.embed',return_value={}),patch('web_app.Pipeline.upload',return_value={'state':'ready'}):
   self.assertEqual(self.post('/api/build?assistant='+aid,{}).status_code,202);self.assertEqual(self.wait(aid)['state'],'done')
  self.assertIn('#226644',self.client.get('/api/admin/assistants/'+aid+'/preview-theme').text)
  self.assertEqual(self.client.get('/a/'+aid+'/sources/'+d['id']).status_code,404)
  self.assertEqual(self.client.get('/a/'+aid+'/sources/'+d['id']+'?preview=1').status_code,200)
  with patch.dict(os.environ,{'APP_ENV':'production','APP_USER_PASSWORD':'user','APP_ADMIN_PASSWORD':'admin','APP_SESSION_SECRET':'offline-secret'}):
   from web_app import create_app
   app=create_app(self.path,testing=True);client=app.test_client();client.get('/login',base_url='https://example.com')
   with client.session_transaction(base_url='https://example.com') as session:token=session['csrf']
   client.post('/login',base_url='https://example.com',json={'csrf':token,'password':'user'})
   self.assertEqual(client.get('/api/admin/assistants/'+aid+'/preview-theme',base_url='https://example.com').status_code,403)
   self.assertEqual(client.get('/api/admin/assistants/'+aid+'/editor',base_url='https://example.com').status_code,403)
   self.assertEqual(client.get('/a/'+aid+'/sources/'+d['id']+'?preview=1',base_url='https://example.com').status_code,404)
 def test_mvp_upload_update_delete_without_manual_review(self):
  docs=DocumentStore(self.catalog,self.default);old=next(x for x in docs.listing() if x['origin']=='official');old_text=docs.preview(old['id'])['text'];record=self.catalog.get(self.default)
  response=self.client.post('/api/admin/assistants/'+self.default+'/documents/'+old['id']+'/update',data={'file':(io.BytesIO('新版资料：办理时间为工作日。'.encode()),'更新资料.txt')},headers={'X-CSRF-Token':self.csrf()})
  self.assertEqual(response.status_code,200,response.json);new=response.json
  self.assertEqual(new['status'],'approved');self.assertEqual(docs.get(new['id'])['replaces'],old['id'])
  path,_=docs.prepare_version();self.assertTrue(any(x['document_id']=='upload-'+new['id'] for x in Pipeline(path).rows))
  self.assertEqual(self.catalog.get(self.default)['active_path'],record['active_path']);self.assertEqual(docs.preview(old['id'])['text'],old_text)
  bad=self.client.post('/api/admin/assistants/'+self.default+'/documents/'+new['id']+'/update',data={'file':(io.BytesIO(b'bad PDF'),'bad.pdf')},headers={'X-CSRF-Token':self.csrf()})
  self.assertEqual(bad.status_code,400);self.assertFalse(docs.get(new['id']).get('removed',False))
  self.assertTrue(docs.change(new['id'],'delete')['ok']);self.assertTrue(docs.get(new['id'])['disabled'])
  attachment=self.client.get(old['original_url']);self.assertEqual(attachment.status_code,200);self.assertIn('attachment',attachment.headers['Content-Disposition']);attachment.close()
 def test_mvp_candidate_switch_does_not_invalidate_built_chunks(self):
  aid=self.create()['id'];uploaded=self.upload(aid);docs=DocumentStore(self.catalog,aid)
  with patch('web_app.Pipeline.embed',return_value={}),patch('web_app.Pipeline.upload',return_value={'state':'ready'}):
   self.assertEqual(self.post('/api/build?assistant='+aid,{}).status_code,202);self.assertEqual(self.wait(aid)['state'],'done')
  candidate=self.catalog.get(aid)['candidate_path']
  docs.change(uploaded['id'],'disable');self.assertEqual(self.catalog.get(aid)['candidate_path'],candidate)
  row=next(x for x in docs.listing() if x['id']==uploaded['id']);self.assertEqual(row['build_state'],'built');self.assertEqual(row['built_chunks'],2);self.assertTrue(row['disabled'])
  docs.change(uploaded['id'],'enable');self.assertFalse(docs.get(uploaded['id'])['disabled'])
  self.client.get('/api/admin/assistants/'+aid+'/preview')
  with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'offline','SUPABASE_URL':'https://fake.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_offline'}):self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,200)
