import json,unittest
from unittest.mock import patch
import test_rag_pipeline
from rag_pipeline import PipelineError,ServiceConnectionError
class LocalReadFallbackTests(unittest.TestCase):
 setUp=test_rag_pipeline.PipelineTests.setUp
 def ready(self):
  self.p.cfg['database']['local_read_fallback']=True
  with patch.object(self.p,'embed_texts',side_effect=lambda texts:[[1,0,0] for _ in texts]):self.p.embed()
 def test_network_failure_uses_exact_current_vectors(self):
  self.ready()
  with patch.object(self.p,'embed_texts',return_value=[[1,0,0]]),patch.object(self.p,'db',side_effect=ServiceConnectionError('network')):
   out=self.p.search('充电宝');self.assertEqual(out['retrieval_backend'],'local_vector_cache');self.assertTrue(out['evidence'])
   self.assertLessEqual(len(out['evidence']),self.p.cfg['retrieval']['context_top_k'])
 def test_disabled_or_incomplete_or_mismatched_cache_fails_closed(self):
  self.ready()
  with patch.object(self.p,'embed_texts',return_value=[[1,0,0]]),patch.object(self.p,'db',side_effect=ServiceConnectionError('network')):
   self.p.cfg['database']['local_read_fallback']=False
   with self.assertRaises(ServiceConnectionError):self.p.search('充电宝')
   self.p.cfg['database']['local_read_fallback']=True
   cache=self.p.cache();cache['vectors'].pop(self.p.rows[0]['chunk_id']);self.p.save_cache(cache)
   with self.assertRaises(PipelineError):self.p.search('充电宝')
   cache['index_id']='another-version';self.p.save_cache(cache)
   with self.assertRaises(PipelineError):self.p.search('充电宝')
 def test_http_errors_are_not_silently_replaced(self):
  self.ready()
  with patch.object(self.p,'embed_texts',return_value=[[1,0,0]]),patch.object(self.p,'db',side_effect=PipelineError('HTTP 403')):
   with self.assertRaisesRegex(PipelineError,'HTTP 403'):self.p.search('充电宝')
 def test_input_hash_mismatch_is_rejected(self):
  self.ready();cache=self.p.cache();cache['vectors'][self.p.rows[0]['chunk_id']]['input_sha256']='wrong';self.p.save_cache(cache)
  with self.assertRaises(PipelineError):self.p.local_vector_search([1,0,0])
