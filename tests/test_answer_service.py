import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from answer_service import AnswerService
from rag_pipeline import PipelineError
ROOT=Path(__file__).resolve().parents[1]

class AnswerTests(unittest.TestCase):
    def setUp(self):
        row={'chunk_id':'one','document_id':'doc','source_version_sha256':'version',
             'title':'测试资料','locator':'第一条','rule_type':'test','issued_date':None,
             'published_date':None,'effective_date':None,'validity_note':'测试',
             'source_url':'https://example.com/official','embedding_text':'第一条 物品必须满足规定条件才能携带。',
             'context_chunk_ids':[],'ingestion_allowed':True,'retrieval_eligible':True}
        cfg=json.loads((ROOT/'config/rag.railway.json').read_text())
        self.p=SimpleNamespace(root=ROOT,cfg=cfg,index_id='index',context={'one':row},search=Mock(return_value={'index_id':'index','evidence':[{'chunk_id':'one'}]}))
        self.s=AnswerService(self.p)
        self.good={'status':'answered','claims':[{'text':'携带时须满足规定条件。','evidence':[{'id':'S1','quote':'物品必须满足规定条件才能携带。'}]}]}

    def call(self,obj,finish='stop'):
        response={'choices':[{'finish_reason':finish,'message':{'content':json.dumps(obj,ensure_ascii=False)}}],'model':'test','usage':{'total_tokens':12}}
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake'}),patch('answer_service.request',return_value=response) as call:
            result=self.s.answer('能带吗')
        return result,call

    def test_valid_answer_uses_server_source_and_audit(self):
        result,call=self.call(self.good)
        self.assertEqual(result['status'],'answered');self.assertIn('[S1]',result['answer'])
        self.assertEqual(result['sources'][0]['source_url'],'https://example.com/official')
        self.assertFalse(result['semantic_entailment_verified'])
        payload=call.call_args.args[2]
        self.assertEqual(payload['model'],self.p.cfg['answer']['model'])
        self.assertEqual(payload['messages'][0]['content'],self.s.prompt)

    def test_unknown_citation_fails_closed(self):
        obj=copy.deepcopy(self.good);obj['claims'][0]['evidence'][0]['id']='S99'
        result,_=self.call(obj);self.assertEqual(result['status'],'invalid_response');self.assertEqual(result['claims'],[])

    def test_fabricated_quote_fails_closed(self):
        obj=copy.deepcopy(self.good);obj['claims'][0]['evidence'][0]['quote']='这里是模型编造的根本不存在的政策。'
        self.assertEqual(self.call(obj)[0]['status'],'invalid_response')

    def test_uncited_and_empty_answers_fail(self):
        for obj in [{'status':'answered','claims':[]}, {'status':'answered','claims':[{'text':'无依据','evidence':[]}]}]:
            self.assertEqual(self.call(obj)[0]['status'],'invalid_response')

    def test_no_evidence_never_calls_generation(self):
        self.p.search.return_value={'index_id':'index','evidence':[]}
        with patch('answer_service.request',side_effect=AssertionError('network')):
            result=self.s.answer('实时余票')
        self.assertEqual(result['status'],'insufficient');self.assertFalse(result['generation_called'])

    def test_insufficient_answer_is_configured_not_unchecked_model_prose(self):
        result,_=self.call({'status':'insufficient','claims':[]})
        self.assertEqual(result['answer'],self.p.cfg['answer']['messages']['insufficient'])

    def test_truncation_and_bad_json_are_blocked(self):
        self.assertEqual(self.call(self.good,'length')[0]['status'],'invalid_response')
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake'}),patch('answer_service.request',return_value={'choices':[{'finish_reason':'stop','message':{'content':'bad JSON'}}]}):
            self.assertEqual(self.s.answer('test')['status'],'invalid_response')

    def test_quarantined_source_and_cross_version_rejected(self):
        self.p.context['one']['ingestion_allowed']=False
        with self.assertRaises(PipelineError):self.s.answer('test')
        self.p.context['one']['ingestion_allowed']=True
        self.p.context['one']['context_chunk_ids']=['two']
        self.p.context['two']={**self.p.context['one'],'source_version_sha256':'other'}
        with self.assertRaises(PipelineError):self.s.answer('test')

    def test_protected_parameters_and_question_limits(self):
        self.p.cfg['answer']['parameters']['messages']=[]
        with self.assertRaises(PipelineError):AnswerService(self.p)
        with self.assertRaises(PipelineError):self.s.answer('x'*3000)

    def test_question_injection_is_kept_in_data_not_system(self):
        _,call=self.call(self.good)
        self.assertEqual(len(call.call_args.args[2]['messages']),2)
        self.assertIn('question',json.loads(call.call_args.args[2]['messages'][1]['content']))

    def test_synthesized_paragraph_can_cite_multiple_sources_without_showing_quotes(self):
        self.p.context['two']={**self.p.context['one'],'chunk_id':'two','document_id':'second',
            'embedding_text':'第二条 携带前应当完成物品登记手续。','title':'补充资料'}
        self.p.search.return_value={'index_id':'index','evidence':[{'chunk_id':'one'},{'chunk_id':'two'}]}
        text='符合规定条件并完成登记后，就可以携带。'
        obj={'status':'answered','claims':[{'text':text,'evidence':[
            {'id':'S1','quote':'物品必须满足规定条件才能携带。'},
            {'id':'S2','quote':'携带前应当完成物品登记手续。'}]}]}
        result,_=self.call(obj)
        self.assertEqual(result['status'],'answered');self.assertEqual(result['answer'],text+' [S1][S2]')
        self.assertEqual(len(result['sources']),2)
        for ref in obj['claims'][0]['evidence']:
            self.assertNotIn(ref['quote'],result['answer'])
            self.assertIn(ref,result['claims'][0]['evidence'])
        obj['claims'][0]['evidence'][1]['quote']='编造的补充规定不能作为可靠证据。'
        self.assertEqual(self.call(obj)[0]['status'],'invalid_response')

    def test_failure_diagnostics_distinguish_no_evidence_model_refusal_and_bad_quote(self):
        result,_=self.call({'status':'insufficient','claims':[]})
        self.assertEqual(result['answerability_reason'],'model_insufficient')
        bad=copy.deepcopy(self.good);bad['claims'][0]['evidence'][0]['quote']='并不存在的引文不能当作证据来源。'
        result,_=self.call(bad)
        self.assertEqual(result['validation_failure_reason'],'citation_quote_mismatch')
        self.assertNotIn('并不存在',json.dumps(result,ensure_ascii=False))
        self.p.search.return_value={'index_id':'index','evidence':[]}
        result=self.s.answer('测试')
        self.assertEqual(result['answerability_reason'],'no_evidence')

if __name__=='__main__':unittest.main()
