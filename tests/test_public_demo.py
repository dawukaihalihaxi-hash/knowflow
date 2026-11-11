"""Public portfolio mode must allow reading and real ask routing, never management writes."""
import os,unittest
from unittest.mock import patch
import test_web_app
from web_app import create_app

class PublicDemoTests(unittest.TestCase):
 setUp=test_web_app.WebTests.setUp
 def public_client(self):
  env=patch.dict(os.environ,{'APP_ENV':'production','APP_PUBLIC_DEMO':'1','APP_ADMIN_PASSWORD':'offline-admin','APP_USER_PASSWORD':'offline-user','APP_SESSION_SECRET':'offline-session'});env.start();self.addCleanup(env.stop)
  return create_app(self.path,testing=True).test_client()
 def test_anonymous_can_read_curated_platform_but_not_privileged_configuration(self):
  c=self.public_client();origin='https://demo.example'
  self.assertEqual(c.get('/',base_url=origin).location,'/admin')
  for path in ('/admin','/settings','/api/admin/assistants','/api/admin/assistants/chuxingtong/editor','/api/admin/assistants/chuxingtong/documents','/api/admin/assistants/chuxingtong/documents/mot-2022-37','/api/build?assistant=chuxingtong'):
   response=c.get(path,base_url=origin);self.assertEqual(response.status_code,200,path)
  for path in ('/setup','/api/settings','/api/admin/runtime','/api/admin/assistants/chuxingtong/traces/unknown'):
   self.assertEqual(c.get(path,base_url=origin).status_code,403,path)
 def test_management_writes_are_blocked_including_authenticated_admin(self):
  c=self.public_client();origin='https://demo.example';c.get('/login',base_url=origin)
  with c.session_transaction(base_url=origin) as s:token=s['csrf'];s['role']='admin'
  for path in ('/api/admin/assistants','/api/admin/assistants/chuxingtong/delete','/api/admin/assistants/chuxingtong/documents','/api/admin/assistants/chuxingtong/documents/mot-2022-37/action','/api/settings','/api/build','/api/credentials','/api/connection'):
   response=c.post(path,json={},headers={'X-CSRF-Token':token},base_url=origin);self.assertEqual(response.status_code,403,path);self.assertIn('固定知识快照',response.json['error'])
 def test_question_route_remains_available_without_calling_models_during_test(self):
  c=self.public_client();origin='https://demo.example';c.get('/admin',base_url=origin)
  with c.session_transaction(base_url=origin) as s:token=s['csrf']
  with patch('web_app.AnswerService.answer',return_value={'status':'insufficient','answer':'offline test','sources':[]}) as mocked:
   response=c.post('/api/assistants/chuxingtong/ask',json={'question':'测试'},headers={'X-CSRF-Token':token},base_url=origin)
  self.assertEqual(response.status_code,200,response.json);mocked.assert_called_once()
