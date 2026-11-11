"""Anonymous isolation, durable restart, sample protection and bounded paid work; offline only."""
import copy,gzip,io,json,os,threading,time,unittest
from unittest.mock import patch
import test_web_app
from web_app import create_app
from trial_service import CloudWorkspace
from rag_pipeline import Pipeline

class Backend:
 def __init__(self):self.state=None;self.files={};self.used={};self.lock=threading.RLock();self.lease=None
 def db(self,path,method='GET',payload=None,prefer=None):
  with self.lock:
   if path.startswith('knowledge_indexes?'):return []
   if path.startswith('knowflow_trial_workspace?'):return [copy.deepcopy(self.state)] if self.state else []
   if not path.startswith('rpc/knowflow_trial_'):raise AssertionError('Unexpected live database request: '+path)
   p=payload;name=path.split('_')[-1]
   if name=='lock':
    if self.lease and self.lease!=p['p_token']:return False
    self.lease=p['p_token'];return True
   if name=='unlock':
    if self.lease==p['p_token']:self.lease=None
    return None
   if name=='commit':
    assert self.lease==p['p_token'];self.state={'manifest':copy.deepcopy(p['p_manifest']),'revision':1+(self.state or {}).get('revision',0)};return self.state['revision']
   if name=='consume':
    checks=p['p_checks'];keys=[(c['subject'],c['period'],c['kind']) for c in checks]
    if any(self.used.get(k,0)+c['amount']>c['limit'] for k,c in zip(keys,checks)):return False
    for k,c in zip(keys,checks):self.used[k]=self.used.get(k,0)+c['amount']
    return True
   raise AssertionError(name)
 def blob(self,cloud,digest,data=None):
  if data is not None:self.files[digest]=data;return b'{}'
  return self.files[digest]

class TrialTests(unittest.TestCase):
 setUp=test_web_app.WebTests.setUp
 origin='https://trial.example'
 def start(self):
  self.backend=Backend()
  self.addCleanup(patch.stopall)
  patch.dict(os.environ,{'APP_ENV':'production','APP_PUBLIC_TRIAL':'1','APP_PUBLIC_DEMO':'0','APP_ADMIN_PASSWORD':'offline-admin','APP_USER_PASSWORD':'offline-user','APP_SESSION_SECRET':'offline-session'}).start()
  patch.object(Pipeline,'db',side_effect=self.backend.db).start()
  b=self.backend
  patch.object(CloudWorkspace,'blob',lambda cloud,digest,data=None:b.blob(cloud,digest,data)).start()
  self.app=create_app(self.path,testing=True)
  self.a=self.app.test_client();self.b=self.app.test_client()
 def get(self,c,url):return c.get(url,base_url=self.origin)
 def post(self,c,url,data=None,**kw):
  self.get(c,'/api/session')
  with c.session_transaction(base_url=self.origin) as session:token=session['csrf']
  return c.post(url,base_url=self.origin,json=data,headers={'X-CSRF-Token':token},**kw)
 def create(self,c):
  r=self.post(c,'/api/admin/assistants',{'name':'访客助手','company':'离线测试','unified_flow':True})
  self.assertEqual(r.status_code,201,r.json);return r.json['id']
 def upload(self,c,aid,text='离线测试资料。',name='test.txt'):
  return self.multipart(c,'/api/admin/assistants/'+aid+'/documents',name,text.encode())
 def multipart(self,c,url,name,raw):
  self.get(c,'/api/session')
  with c.session_transaction(base_url=self.origin) as session:token=session['csrf']
  return c.post(url,base_url=self.origin,data={'file':(io.BytesIO(raw),name)},headers={'X-CSRF-Token':token})
 def test_owner_isolation_sample_and_admin(self):
  self.start();aid=self.create(self.a);bid=self.create(self.b)
  self.assertEqual(self.post(self.a,'/api/admin/assistants',{'name':'two','company':'x'}).status_code,409)
  listed=self.get(self.b,'/api/admin/assistants').json['assistants'];self.assertNotIn(aid,[r['id'] for r in listed]);self.assertIn(bid,[r['id'] for r in listed])
  for path in ('/editor','/documents','/preview','/preview-brand','/configuration'):
   self.assertEqual(self.get(self.b,'/api/admin/assistants/'+aid+path).status_code,403,path)
  for path in ('/api/build','/api/build?assistant=chuxingtong','/api/admin/assistants/chuxingtong/delete','/api/admin/assistants/chuxingtong/restore','/api/admin/assistants/chuxingtong/editor'):
   self.assertEqual(self.post(self.a,path,{}).status_code,403,path)
  self.assertEqual(self.get(self.a,'/api/admin/assistants/chuxingtong/documents').status_code,200)
  self.assertEqual(self.get(self.a,'/api/settings').status_code,403)
  self.assertTrue(self.get(self.a,'/settings').data.find(b'data-demo-readonly="true"')>=0)
  self.get(self.a,'/login')
  with self.a.session_transaction(base_url=self.origin) as s:token=s['csrf']
  r=self.a.post('/login',base_url=self.origin,json={'password':'offline-admin','csrf':token});self.assertEqual(r.status_code,200)
  self.assertEqual(self.get(self.a,'/settings').status_code,200)
  self.assertIn(b'data-demo-readonly="false"',self.get(self.a,'/settings').data)
 def test_upload_boundaries_and_durable_restore_after_local_loss(self):
  self.start();aid=self.create(self.a)
  for i in range(10):self.assertEqual(self.upload(self.a,aid,'这是测试资料的完整内容，用来验证上传及持久保存。'+str(i),str(i)+'.txt').status_code,201)
  self.assertEqual(self.upload(self.a,aid,'第11份').status_code,429)
  self.assertEqual(self.multipart(self.a,'/api/admin/assistants/'+aid+'/documents','big.txt',b'x'*(5*1024*1024+1)).status_code,413)
  import shutil
  shutil.rmtree(self.root/'local-data')
  app=create_app(self.path,testing=True);c=app.test_client()
  # Retain the browser credential across the process restart.
  with self.a.session_transaction(base_url=self.origin) as s:identity=dict(s)
  with c.session_transaction(base_url=self.origin) as s:s.update(identity)
  self.assertEqual(len(self.get(c,'/api/admin/assistants/'+aid+'/documents').json['documents']),10)
  self.assertEqual(self.post(c,'/api/admin/assistants',{'name':'two','company':'x'}).status_code,409)
 def test_question_quota_persists_and_blocks_before_model_call(self):
  self.start()
  from datetime import timedelta
  import trial_service
  clock=trial_service.now()
  with patch('web_app.AnswerService.answer',return_value={'answer':'offline','sources':[]}) as answer:
   for i in range(20):
    with patch('trial_service.now',return_value=clock+timedelta(minutes=i)):
     r=self.post(self.a,'/api/assistants/chuxingtong/ask',{'question':'测试'})
     self.assertEqual(r.status_code,200,r.json)
   with patch('trial_service.now',return_value=clock+timedelta(minutes=21)):
    self.assertEqual(self.post(self.a,'/api/assistants/chuxingtong/ask',{'question':'测试'}).status_code,429)
   self.assertEqual(answer.call_count,20)
 def test_build_preview_publish_and_restart_without_real_calls(self):
  self.start();aid=self.create(self.a);self.assertEqual(self.upload(self.a,aid,'资料内容。'*500).status_code,201)
  state=self.get(self.a,'/api/admin/assistants/'+aid+'/editor').json
  body={k:state[k] for k in ('revision','web','top_k','business')};body['models']={k:state['models'][k] for k in ('answer','embedding')};body['web']['description']=''
  self.assertEqual(self.post(self.a,'/api/admin/assistants/'+aid+'/editor',body).status_code,200)
  self.assertEqual(self.get(self.a,'/admin/assistants/'+aid+'/preview-chat').status_code,400)
  def embed(p,progress=None):
   vectors={r['chunk_id']:{'input_sha256':__import__('hashlib').sha256(r['embedding_text'].encode()).hexdigest(),'vector':[1.0]*p.profile['dimensions']} for r in p.rows}
   p.save_cache({'index_id':p.index_id,'profile':p.profile,'vectors':vectors});return {'new_vectors':len(vectors)}
  with patch.object(Pipeline,'embed',embed),patch.object(Pipeline,'upload',return_value={'state':'ready','written_rows':2}),patch.dict(os.environ,{'DASHSCOPE_API_KEY':'offline','SUPABASE_URL':'https://offline.example','SUPABASE_SECRET_KEY':'offline'}):
   r=self.post(self.a,'/api/build?assistant='+aid,{});self.assertEqual(r.status_code,202,r.json)
   for _ in range(200):
    time.sleep(.02)
    record=json.loads((self.root/'local-data/assistant-registry.json').read_text())['assistants'][-1]
    if record['job']['state']!='running':break
   self.assertEqual(record['job']['state'],'done',record['job'])
   for _ in range(100):
    if not self.backend.lease:break
    time.sleep(.02)
   self.assertFalse(record['enabled'])
   self.assertEqual(self.get(self.a,'/api/admin/assistants/'+aid+'/preview').status_code,200)
   self.assertEqual(self.post(self.a,'/api/admin/assistants/'+aid+'/activate',{}).status_code,200)
   self.assertEqual(self.get(self.b,'/a/'+aid).status_code,200)
   self.assertEqual(self.get(self.b,'/api/admin/assistants/'+aid+'/documents').status_code,403)
   doc=self.get(self.a,'/api/admin/assistants/'+aid+'/documents').json['documents'][0]
   self.assertEqual(self.get(self.b,'/a/'+aid+'/sources/'+doc['id']).status_code,403)

 def test_ready_sample_reuses_existing_index_without_embedding(self):
  self.start()
  with self.a.session_transaction(base_url=self.origin) as session:session['role']='admin'
  sample=Pipeline(self.path)
  original=self.backend.db
  def db(path,*args,**kwargs):
   if path.startswith('knowledge_indexes?'):return [{'state':'ready','expected_count':len(sample.rows),'profile':sample.profile}]
   if path.startswith('knowledge_chunks?'):return [{'chunk_id':r['chunk_id']} for r in sample.rows]
   return original(path,*args,**kwargs)
  with patch.object(Pipeline,'db',side_effect=db),patch.object(Pipeline,'embed',side_effect=AssertionError('Existing sample must not be embedded')) as embedded:
   result=self.post(self.a,'/api/build?assistant=chuxingtong',{});self.assertEqual(result.status_code,202,result.json)
   for _ in range(300):
    time.sleep(.02)
    records=json.loads((self.root/'local-data/assistant-registry.json').read_text())['assistants'];record=records[0]
    if record['job']['state']!='running' and not self.backend.lease:break
   self.assertEqual(record['job']['state'],'done',record['job'])
   self.assertEqual(record['job']['metrics']['new_vectors'],0);self.assertEqual(record['active_index'],sample.index_id)
   embedded.assert_not_called()
 def test_failed_cloud_save_is_not_acknowledged_or_applied(self):
  self.start()
  original=self.backend.db
  def db(path,*args,**kwargs):
   if path=='rpc/knowflow_trial_commit':raise RuntimeError('offline storage unavailable')
   return original(path,*args,**kwargs)
  with patch.object(Pipeline,'db',side_effect=db):
   response=self.post(self.a,'/api/admin/assistants',{'name':'failed','company':'test'})
   self.assertEqual(response.status_code,503)
  listed=self.get(self.a,'/api/admin/assistants').json['assistants']
  self.assertEqual(len(listed),1)
  self.create(self.a)

if __name__=='__main__':unittest.main()
