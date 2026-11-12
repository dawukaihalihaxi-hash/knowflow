import copy,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_answer_service as legacy
from answer_service import AnswerService,normalized
from evidence_protocol import EvidenceProtocol
from rag_pipeline import PipelineError

class ProtocolTests(unittest.TestCase):
    call=legacy.AnswerTests.call
    def setUp(self):
        legacy.AnswerTests.setUp(self);self.s=AnswerService(self.p,protocol='paragraphs-v1')
        self.good={'status':'answered','claims':[{'text':'携带时须满足规定条件。','evidence':['S1.P1']}]}
    # These end-to-end cases exercise only the new wire format.
    def test_valid_answer_uses_server_source_and_audit(self):
        result,call=self.call(self.good)
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['claims'][0]['evidence'],[{'id':'S1','quote':self.p.context['one']['embedding_text']}])
        payload=call.call_args.args[2]
        self.assertEqual(payload['response_format']['type'],'json_schema')
        self.assertTrue(payload['response_format']['json_schema']['strict'])
        self.assertEqual(payload['response_format']['json_schema']['schema']['properties']['claims']['items']['properties']['evidence']['items']['enum'],['S1.P1'])
    def test_unknown_citation_fails_closed(self):
        for refs in [['S13.P1'],['S1.P99'],[{'id':'S1','quote':'某个文字'}]]:
            obj=copy.deepcopy(self.good);obj['claims'][0]['evidence']=refs
            result,_=self.call(obj);self.assertEqual(result['status'],'invalid_response');self.assertEqual(result['claims'],[])
            self.assertEqual(self.s.last_trace['validation_error'],'Unknown citation')
    def test_literal_paragraphs_conditions_and_version_binding(self):
        text='标题\n\n必须满足甲条件才可携带此物品。\n\n但不得超过两件，并且不能托运。\n\n尾注'
        evidence=[{'id':'S1','text':text,'source_version_sha256':'v1'},{'id':'S2','text':'另一版本的条件不同，不能互相混用。','source_version_sha256':'v2'}]
        before=copy.deepcopy(evidence);p=EvidenceProtocol(evidence,12,10)
        resolved=p.resolve({'status':'answered','claims':[{'text':'测试','evidence':['S1.P2','S2.P1']}]})
        self.assertEqual(resolved['claims'][0]['evidence'][0]['id'],'S1')
        self.assertEqual(resolved['claims'][0]['evidence'][1]['id'],'S2')
        self.assertEqual(''.join(normalized(x['text']) for x in p.evidence[0]['paragraphs']),normalized(text))
        self.assertEqual(evidence,before)
        for ref in p.refs.values():self.assertIn(normalized(ref['quote']),normalized(next(x['text'] for x in evidence if x['id']==ref['id'])))
    def test_duplicate_refs_do_not_duplicate_quotes(self):
        obj=copy.deepcopy(self.good);obj['claims'][0]['evidence']*=2
        result,_=self.call(obj);self.assertEqual(len(result['claims'][0]['evidence']),1)

if __name__=='__main__':unittest.main()
