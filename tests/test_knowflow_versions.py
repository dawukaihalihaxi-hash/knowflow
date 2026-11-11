"""Offline acceptance contracts. All embedding/database operations are mocked."""
import json,os,time,unittest
from unittest.mock import patch
import test_assistant_lifecycle as lifecycle
from document_store import DocumentStore
from rag_pipeline import Pipeline,PipelineError
from assistant_catalog import read

class KnowFlowVersions(unittest.TestCase):
 setUp=lifecycle.LifecycleTests.setUp
 csrf=lifecycle.LifecycleTests.csrf
 post=lifecycle.LifecycleTests.post
 create=lifecycle.LifecycleTests.create
 upload=lifecycle.LifecycleTests.upload
 def build(self,aid,fail=False):
  with patch('web_app.Pipeline.embed',side_effect=PipelineError('offline failure') if fail else None,return_value={}),patch('web_app.Pipeline.upload',return_value={'state':'ready'}):
   response=self.post('/api/build?assistant='+aid,{});self.assertEqual(response.status_code,202,response.json)
   for _ in range(300):
    if self.catalog.get(aid)['job']['state']!='running':break
    time.sleep(.01)
   self.assertNotEqual(self.catalog.get(aid)['job']['state'],'running')
 def test_official_inventory_remove_restore_and_versions(self):
  docs=DocumentStore(self.catalog,self.default);rows=docs.listing()
  self.assertEqual(len(rows),8);self.assertEqual(sum(d['status']=='quarantined' for d in rows),1);self.assertEqual(sum(d['active_chunks'] for d in rows),202)
  original=Pipeline(self.path);did=rows[0]['id'];docs.change(did,'remove');path,_=docs.prepare_version()
  self.assertFalse(any(x['document_id']==did for x in Pipeline(path).rows))
  self.assertEqual(len(Pipeline(self.path).rows),len(original.rows))
  docs.change(did,'restore');path,_=docs.prepare_version();self.assertEqual(len(Pipeline(path).rows),202)
  docs.change(did,'delete');self.assertTrue(docs.get(did)['disabled']);self.assertTrue(docs.get(did)['removed'])
 def test_retry_reuses_snapshot_and_requires_preview_before_publish(self):
  aid=self.default;old=self.catalog.get(aid)['active_path'];self.build(aid,fail=True)
  failed=self.catalog.get(aid);self.assertEqual(failed['active_path'],old);self.build(aid)
  current=self.catalog.get(aid);self.assertEqual(current['build_path'],failed['build_path'])
  self.assertEqual(current['active_path'],old)
  self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,400)
  self.assertEqual(self.client.get('/api/admin/assistants/'+aid+'/preview').status_code,200)
  self.assertEqual(self.client.get('/admin/assistants/'+aid+'/preview-chat').status_code,200)
  with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake','SUPABASE_URL':'https://fake.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_fake'}):
   self.assertEqual(self.post('/api/admin/assistants/'+aid+'/activate',{}).status_code,200)
  self.assertNotEqual(self.catalog.get(aid)['active_path'],old)
 def test_changed_documents_invalidate_candidate(self):
  self.build(self.default);docs=DocumentStore(self.catalog,self.default);docs.change(docs.all()[0]['id'],'remove')
  self.assertIsNone(self.catalog.get(self.default).get('candidate_path'))
  self.assertEqual(self.client.get('/api/admin/assistants/'+self.default+'/preview').status_code,400)
 def test_default_role_top_k_allowed_engineering_and_prompt_rejected(self):
  with patch.dict(os.environ,{'APP_ALLOW_ADVANCED_SETTINGS':'0'}):
   data=self.client.get('/api/settings').json;data['config']['retrieval']['context_top_k']=8
   self.assertEqual(self.post('/api/settings',data).status_code,200)
   data=self.client.get('/api/settings').json;data['config']['embedding']['dimensions']=512
   self.assertEqual(self.post('/api/settings',data).status_code,400)
   data=self.client.get('/api/settings').json;data['prompt']+='overwrite'
   self.assertEqual(self.post('/api/settings',data).status_code,400)
   data=self.client.get('/api/settings').json;data['config']['retrieval']['context_top_k']=101
   self.assertEqual(self.post('/api/settings',data).status_code,400)
 def test_business_settings_isolated_and_draft_cannot_preview_chat(self):
  a=self.create();b=self.create();data=self.client.get('/api/settings?assistant='+a['id']).json
  data['config']['business']['scope']='仅处理内部报销'
  self.assertEqual(self.post('/api/settings?assistant='+a['id'],data).status_code,200)
  self.assertEqual(self.client.get('/api/settings?assistant='+b['id']).json['config']['business']['scope'],read(self.root/'config/business-defaults.json')['scope'])
  self.assertEqual(self.client.get('/a/'+a['id']).status_code,403)
  self.assertEqual(self.client.get('/admin/assistants/'+a['id']+'/preview-chat').status_code,400)
  self.assertEqual(self.post('/api/admin/assistants/'+a['id']+'/preview-ask',{'question':'离线'}).status_code,400)
 def test_disabled_document_excluded_from_every_retrieval_route(self):
  p=Pipeline(self.path);did=p.rows[0]['document_id'];p.blocked_documents={did}
  with patch.object(p,'embed_texts',return_value=[[1.0]+[0.0]*1023]),patch.object(p,'db',return_value=[{'chunk_id':p.rows[0]['chunk_id'],'similarity':1}]):
   result=p.search('铁路旅客运输规程')
  self.assertFalse(any(x['document_id']==did for x in result['evidence']))
