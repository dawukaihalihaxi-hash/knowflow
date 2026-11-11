import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from rag_pipeline import Pipeline,PipelineError,bm25,fuse,validate_vector
ROOT=Path(__file__).resolve().parents[1]

class PipelineTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        for folder in ('config','artifacts'):
            shutil.copytree(ROOT/folder,self.root/folder)
        self.path=self.root/'config/rag.railway.json'
        self.cfg=json.loads(self.path.read_text());self.cfg['embedding']['dimensions']=3
        self.cfg['embedding']['batch_size']=100
        self.path.write_text(json.dumps(self.cfg))
        self.p=Pipeline(self.path)

    def test_plan_never_calls_network(self):
        with patch('rag_pipeline.request',side_effect=AssertionError('network')):
            self.assertEqual(self.p.plan()['eligible_chunks'],202)

    def test_mock_embedding_response_reordered_and_validated(self):
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake'}),patch('rag_pipeline.request',return_value={'data':[{'index':1,'embedding':[0,1,0]},{'index':0,'embedding':[1,0,0]}]}):
            self.assertEqual(self.p.embed_texts(['a','b']),[[1,0,0],[0,1,0]])
        with self.assertRaises(PipelineError):validate_vector([0,0,0],3)
        with self.assertRaises(PipelineError):validate_vector([1,float('nan'),0],3)

    def test_missing_key_fails_without_network(self):
        with patch.dict(os.environ,{},clear=True),patch('rag_pipeline.request',side_effect=AssertionError()):
            with self.assertRaisesRegex(PipelineError,'Missing local'):self.p.embed_texts(['a'])

    def test_resume_only_missing_and_reject_changed_profile(self):
        calls=[]
        def fake(texts):calls.append(len(texts));return [[1,0,0] for _ in texts]
        with patch.object(self.p,'embed_texts',side_effect=fake):
            self.assertEqual(self.p.embed()['new_vectors'],202)
            self.assertEqual(self.p.embed()['new_vectors'],0)
        self.assertEqual(calls,[100,100,2])
        self.cfg['embedding']['model']='other-model';self.path.write_text(json.dumps(self.cfg))
        with self.assertRaisesRegex(PipelineError,'different model'):Pipeline(self.path).cache()

    def test_incomplete_cache_upload_sends_nothing(self):
        with patch.object(self.p,'db',side_effect=AssertionError('network')):
            with self.assertRaisesRegex(PipelineError,'Complete matching'):self.p.upload()

    def test_upload_and_tenant_scope(self):
        with patch.object(self.p,'embed_texts',side_effect=lambda ts:[[1,0,0] for t in ts]):self.p.embed()
        calls=[]
        def db(path,method='GET',payload=None,prefer=None):
            calls.append((path,payload))
            if 'register' in path:return 'building'
            if 'finalize' in path:return {'state':'ready','count':202}
        with patch.object(self.p,'db',side_effect=db):self.assertEqual(self.p.upload()['count'],202)
        records=[r for path,p in calls if path.startswith('knowledge_chunks') for r in p]
        self.assertEqual(len(records),202)
        self.assertTrue(all(r['tenant_id']==self.cfg['tenant_id'] and r['index_id']==self.p.index_id for r in records))
        self.assertFalse(any(r['payload']['document_id']=='cr-2026-rules' for r in records))

    def test_chinese_lexical_and_budget(self):
        ranked=bm25('100Wh充电宝',self.p.rows)
        self.assertIn('充电宝',ranked[0][0]['body'])
        with patch.object(self.p,'embed_texts',return_value=[[1,0,0]]),patch.object(self.p,'db',return_value=[]):
            result=self.p.search('充电宝')
        self.assertGreater(len(result['evidence']),0)
        self.assertLessEqual(len(result['evidence']),5)
        self.assertLessEqual(result['context_characters'],8000)
        self.assertFalse(result['answer_generated'])

    def test_topk_and_reranker_fail_closed(self):
        self.cfg['retrieval']['vector_top_k']=101;self.path.write_text(json.dumps(self.cfg))
        with self.assertRaises(PipelineError):Pipeline(self.path)
        self.cfg['retrieval']['vector_top_k']=10;self.cfg['retrieval']['rerank_enabled']=True
        self.path.write_text(json.dumps(self.cfg))
        with self.assertRaisesRegex(PipelineError,'not implemented'):Pipeline(self.path)

    def test_new_supabase_secret_is_not_bearer_token(self):
        with patch.dict(os.environ,{'SUPABASE_URL':'https://example.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_fake'}),patch('rag_pipeline.request',return_value=[]) as call:
            self.p.db('knowledge_chunks')
        headers=call.call_args.args[3]
        self.assertEqual(headers['apikey'],'sb_secret_fake')
        self.assertNotIn('Authorization',headers)

    def test_dashboard_url_rejected_before_credentials_sent(self):
        with patch.dict(os.environ,{'SUPABASE_URL':'https://supabase.com/dashboard/project/example','SUPABASE_SECRET_KEY':'fake'}),patch('rag_pipeline.request',side_effect=AssertionError('network')):
            with self.assertRaisesRegex(PipelineError,'API origin'):self.p.db('knowledge_chunks')

    def test_hash_tampering_rejected(self):
        p=self.root/self.cfg['chunks_path'];p.write_text(p.read_text()+'\n')
        with self.assertRaisesRegex(PipelineError,'hash mismatch'):Pipeline(self.path)

if __name__=='__main__':unittest.main()
