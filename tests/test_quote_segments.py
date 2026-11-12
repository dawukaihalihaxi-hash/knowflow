"""Exact paragraph citation recovery; no fuzzy or cross-source repair."""
import copy
import unittest
import test_answer_service

class QuoteSegmentTests(unittest.TestCase):
    call=test_answer_service.AnswerTests.call
    a='第一条 产品适用年龄为6周岁，办理时应当出示有效证件。'
    middle='另有办理时间和地点限制，详见本条的其余内容。'
    b='第二条 不得携带未经登记的物品，申请时应当如实说明用途。'

    def setUp(self):
        test_answer_service.AnswerTests.setUp(self)
        self.p.context['one']['embedding_text']='\n\n'.join((self.a,self.middle,self.b))

    def segmented(self,quote=None):
        return {'status':'answered','claims':[{'text':'结论保持原样。','evidence':[{'id':'S1','quote':quote if quote is not None else self.a+'\n\n'+self.b}]}]}

    def test_noncontiguous_paragraphs_become_independent_verified_refs(self):
        original=self.segmented();unchanged=copy.deepcopy(original)
        result,request=self.call(original)
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['claims'][0]['text'],original['claims'][0]['text'])
        self.assertEqual(result['claims'][0]['evidence'],[{'id':'S1','quote':self.a},{'id':'S1','quote':self.b}])
        self.assertEqual(original,unchanged);self.assertEqual(request.call_count,1)
        self.assertEqual(len(result['sources']),1)

    def test_continuous_quote_keeps_original_shape(self):
        quote=self.a+'\n\n'+self.middle
        result,_=self.call(self.segmented(quote))
        self.assertEqual(result['status'],'answered')
        self.assertEqual(result['claims'][0]['evidence'],[{'id':'S1','quote':quote}])

    def test_missing_paragraph_boundary_does_not_permit_repair(self):
        for separator in ('',' ','\n','……'):
            with self.subTest(separator=separator):
                self.assertEqual(self.call(self.segmented(self.a+separator+self.b))[0]['status'],'invalid_response')

    def test_changes_to_age_negation_conditions_punctuation_and_short_segments_fail(self):
        for second in (self.b.replace('不得','可以'),self.b.replace('未经登记的',''),self.b.replace('，',','),'不存在的原文段落，不可用其他资料替代。','短引文'):
            with self.subTest(second=second):
                self.assertEqual(self.call(self.segmented(self.a+'\n\n'+second))[0]['status'],'invalid_response')
        for first in (self.a.replace('6','5'),self.a.replace('6','６')):
            self.assertEqual(self.call(self.segmented(first+'\n\n'+self.b))[0]['status'],'invalid_response')

    def test_reverse_duplicate_and_overlapping_segments_fail(self):
        for quote in (self.b+'\n\n'+self.a,self.a+'\n\n'+self.a,self.a+'\n\n'+self.a[3:]):
            with self.subTest(quote=quote):
                self.assertEqual(self.call(self.segmented(quote))[0]['status'],'invalid_response')

    def test_source_binding_is_not_changed_to_find_a_match(self):
        self.p.context['two']={**self.p.context['one'],'chunk_id':'two','document_id':'other','embedding_text':self.b}
        self.p.search.return_value={'index_id':'index','evidence':[{'chunk_id':'one'},{'chunk_id':'two'}]}
        obj=self.segmented();obj['claims'][0]['evidence'][0]['id']='S2'
        self.assertEqual(self.call(obj)[0]['status'],'invalid_response')

    def test_invalid_segment_keeps_entire_answer_closed_and_input_unchanged(self):
        obj=self.segmented(self.a+'\n\n'+'虚构条款不在本次证据内，不能放行。');before=copy.deepcopy(obj)
        evidence=self.s.prepare_evidence(self.p.search.return_value)
        with self.assertRaises(ValueError):self.s.validate(obj,evidence)
        self.assertEqual(obj,before)
        result,_=self.call(obj);self.assertEqual(result['claims'],[]);self.assertEqual(result['sources'],[])

    def test_crlf_and_whitespace_boundaries_preserve_literal_segments(self):
        result,_=self.call(self.segmented('  '+self.a+'\r\n \r\n'+self.b+'  '))
        self.assertEqual(result['status'],'answered')
        self.assertEqual([ref['quote'] for ref in result['claims'][0]['evidence']],[self.a,self.b])

if __name__=='__main__':unittest.main()
