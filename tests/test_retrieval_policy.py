import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
import test_rag_pipeline
from rag_pipeline import Pipeline,PipelineError
from retrieval_policy import plan_query,validate_rules
ROOT=Path(__file__).resolve().parents[1]
RULES=json.loads((ROOT/'config/retrieval-rules.railway.json').read_text())
class RetrievalPolicyTests(unittest.TestCase):
 setUp=test_rag_pipeline.PipelineTests.setUp
 def search(self,q):
  self.p.cfg['retrieval']['query_rules']=copy.deepcopy(RULES)
  with patch.object(self.p,'embed_texts',return_value=[[1,0,0]]) as embed,patch.object(self.p,'db',return_value=[]):result=self.p.search(q)
  return result,embed
 def test_paraphrases_cover_identical_complete_approved_catalog(self):
  before=self.p.index_id
  for q in ['有什么违禁品要求吗','禁止携带的物品有哪些？','违禁品有哪些？']:
   with self.subTest(q=q):
    out,embed=self.search(q);self.assertTrue(out['coverage']['complete']);self.assertEqual(len(out['evidence']),3)
    self.assertEqual([len(e['source_chunk_ids']) for e in out['evidence']],[10,5,7])
    self.assertIn('禁止托运和随身携带',embed.call_args.args[0][0])
    text=''.join(e['context_text'] for e in out['evidence'])
    for word in ['枪支','爆炸','管制','香水','100Wh']:self.assertIn(word,text)
    self.assertEqual(self.p.index_id,before);self.assertLessEqual(out['context_characters'],8000)
 def test_limit_intent_does_not_turn_into_prohibited_catalog(self):
  out,_=self.search('有什么限制随身携带的物品？');self.assertEqual(len(out['evidence']),1)
  self.assertTrue(out['evidence'][0]['locator'].startswith('三、'))
 def test_specific_item_and_handling_requests_keep_original_query(self):
  for q in ['100Wh充电宝算违禁品吗？','两瓶香水属于违禁品吗？','查到违禁品有什么处理办法？']:
   with self.subTest(q=q):
    out,embed=self.search(q);self.assertEqual(embed.call_args.args[0],[q]);self.assertEqual(out['coverage']['mode'],'specific')
 def test_top_k_and_character_budget_remain_hard_limits(self):
  self.p.cfg['retrieval']['context_top_k']=1;out,_=self.search('有什么违禁品要求吗');self.assertEqual(len(out['evidence']),1);self.assertFalse(out['coverage']['complete'])
  self.assertIn('context_top_k',[x['decision'] for x in out['trace']['selection']])
  self.p.cfg['retrieval']['context_top_k']=5;self.p.cfg['retrieval']['max_context_characters']=500
  out,_=self.search('违禁品有哪些？');self.assertLessEqual(out['context_characters'],500);self.assertFalse(out['coverage']['complete'])
  self.assertIn('context_budget',[x['decision'] for x in out['trace']['selection']])
 def test_missing_or_quarantined_source_does_not_enter_context(self):
  rules=copy.deepcopy(RULES);rules[0]['groups']=[{'document_id':'cr-2026-rules','locator_prefix':'第一章'}]
  self.p.cfg['retrieval']['query_rules']=rules
  with patch.object(self.p,'embed_texts',return_value=[[1,0,0]]),patch.object(self.p,'db',return_value=[]):out=self.p.search('违禁品有哪些？')
  self.assertEqual(out['evidence'],[]);self.assertFalse(out['coverage']['complete'])
 def test_rule_validation_rejects_bad_shapes_and_duplicates(self):
  for value in [None,{},[{'id':'bad'}],RULES+[RULES[0]]]:
   with self.subTest(value=type(value).__name__):
    with self.assertRaises(ValueError):validate_rules(value)
 def test_derived_group_retains_original_text_and_version(self):
  out,_=self.search('违禁品有哪些？')
  for e in out['evidence']:
   pack=self.p.context[e['chunk_id']]
   for cid in pack['source_chunk_ids']:
    original=self.p.context[cid];self.assertIn(original['body'],pack['body']);self.assertEqual(original['source_version_sha256'],pack['source_version_sha256'])
