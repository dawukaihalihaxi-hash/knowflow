"""Configured grounded-answer layer; no business prompt or model name in core."""
import argparse
import json
import logging
from pathlib import Path
import re
from rag_pipeline import Pipeline, PipelineError, read_env, require, request, safe_path, sha

PROTECTED_PARAMETERS={'model','messages','stream','tools','tool_choice'}

def normalized(text):
    return re.sub(r'\s+','',text)

class AnswerService:
    def __init__(self,pipeline,protocol=None):
        self.pipeline=pipeline
        if protocol not in (None,'paragraphs-v1'):raise PipelineError('Unsupported evidence protocol')
        self.protocol=protocol
        self.last_trace={}
        self.cfg=pipeline.cfg['answer']
        c=self.cfg
        self.prompt=safe_path(pipeline.root,c['prompt_path']).read_text()
        foundation=pipeline.root/'prompts/platform-rules-v1.txt'
        if 'business' in pipeline.cfg and foundation.exists():self.prompt=foundation.read_text()+'\n\n'+self.prompt
        if pipeline.cfg.get('business'):
            self.prompt+='\n\n助手业务设置（受平台基础规则约束）：\n'+json.dumps(pipeline.cfg['business'],ensure_ascii=False)
        if protocol:
            self.prompt+='\n\n'+safe_path(pipeline.root,'prompts/evidence-protocol-v1.txt').read_text()
        self.prompt_sha256=sha(self.prompt.encode())
        if PROTECTED_PARAMETERS.intersection(c['parameters']):
            raise PipelineError('Answer parameters cannot override request identity or enable tools')
        for k in ('max_question_characters','max_claims','minimum_quote_characters'):
            if type(c[k]) is not int or c[k]<1:raise PipelineError('Positive answer limits required')
        for k in ('insufficient','conflict','invalid_response'):
            if not isinstance(c['messages'].get(k),str):raise PipelineError('Configured status messages required')

    def prepare_evidence(self,retrieved):
        records=[];seen=set()
        for result in retrieved['evidence']:
            cid=result['chunk_id']
            if cid in seen:continue
            seen.add(cid)
            row=self.pipeline.context.get(cid)
            if not row or row.get('ingestion_allowed') is not True or row.get('retrieval_eligible') is not True:
                raise PipelineError('Unapproved evidence received')
            context=[]
            for ref in row.get('context_chunk_ids',[]):
                p=self.pipeline.context[ref]
                if p['document_id']!=row['document_id'] or p['source_version_sha256']!=row['source_version_sha256']:
                    raise PipelineError('Evidence context version mismatch')
                context.append(p['body'])
            records.append({'id':f'S{len(records)+1}','chunk_id':cid,'document_id':row['document_id'],
                'title':row['title'],'locator':row['locator'],'rule_type':row['rule_type'],
                'issued_date':row['issued_date'],'published_date':row['published_date'],
                'effective_date':row['effective_date'],'validity_note':row['validity_note'],
                'usage_note':row.get('usage_note'),'source_url':row['source_url'],
                'source_version_sha256':row['source_version_sha256'],
                'text':'\n\n'.join([*context,row['embedding_text']])})
        return records

    def validate(self,obj,evidence):
        if not isinstance(obj,dict) or set(obj)!={'status','claims'}:raise PipelineError('Invalid answer object')
        status=obj['status'];claims=obj['claims']
        if status not in ('answered','insufficient','conflict') or not isinstance(claims,list) or len(claims)>self.cfg['max_claims']:
            raise PipelineError('Invalid answer status or claim count')
        if (status=='answered' and not claims) or (status=='insufficient' and claims):raise PipelineError('Status contradicts claims')
        by_id={x['id']:x for x in evidence}
        checked_claims=[]
        for claim in claims:
            if not isinstance(claim,dict) or set(claim)!={'text','evidence'} or not isinstance(claim['text'],str) or not claim['text'].strip() or re.search(r'https?://',claim['text']):
                raise PipelineError('Invalid claim text')
            refs=claim['evidence']
            if not isinstance(refs,list) or not refs:raise PipelineError('Uncited claim')
            checked_refs=[]
            for ref in refs:
                if not isinstance(ref,dict) or set(ref)!={'id','quote'} or not isinstance(ref['id'],str) or ref['id'] not in by_id or not isinstance(ref['quote'],str):
                    raise PipelineError('Unknown citation')
                checked_refs.extend(self.verified_quote_parts(ref,by_id[ref['id']]['text']))
            checked_claims.append({**claim,'evidence':checked_refs})
        return {**obj,'claims':checked_claims}

    def verified_quote_parts(self,ref,source_text):
        source=normalized(source_text);quote=normalized(ref['quote'])
        minimum=self.cfg['minimum_quote_characters']
        # Preserve already valid quotations, including continuous paragraphs.
        if len(quote)>=minimum and quote in source:return [dict(ref)]
        # Only explicit paragraph boundaries may separate non-contiguous quotes.
        # Each segment stays bound to the same evidence and its original order.
        parts=[part.strip() for part in re.split(r'\n\s*\n',ref['quote']) if part.strip()]
        if len(parts)<2:raise PipelineError('Citation quote is absent or too short')
        checked=[];cursor=0
        for part in parts:
            text=normalized(part)
            if len(text)<minimum:raise PipelineError('Citation quote is absent or too short')
            start=source.find(text,cursor)
            if start<0:raise PipelineError('Citation quote is absent or too short')
            cursor=start+len(text)
            checked.append({'id':ref['id'],'quote':part})
        return checked

    def answer(self,query):
        self.last_trace={'question':query,'index_id':self.pipeline.index_id,'prompt_sha256':self.prompt_sha256}
        try:
            result=self._answer(query);self.last_trace['result']=result;return result
        except PipelineError as error:
            self.last_trace['error']=str(error);raise

    def _answer(self,query):
        if not isinstance(query,str) or not query.strip() or len(query)>self.cfg['max_question_characters']:
            raise PipelineError('Question is empty or exceeds configured length')
        retrieved=self.pipeline.search(query)
        if retrieved.get('index_id')!=self.pipeline.index_id:raise PipelineError('Retrieval index mismatch')
        evidence=self.prepare_evidence(retrieved)
        self.last_trace.update(retrieval=retrieved,evidence=evidence)
        base={'retrieval_backend':retrieved.get('retrieval_backend','supabase'),'index_id':self.pipeline.index_id,'configured_model':self.cfg['model'],
              'prompt_sha256':self.prompt_sha256,'citation_validation':'not_run',
              'semantic_entailment_verified':False}
        if not evidence:
            return {**base,'status':'insufficient','answer':self.cfg['messages']['insufficient'],
                    'claims':[],'sources':[],'generation_called':False,'answerability_reason':'no_evidence'}
        protocol=None
        if self.protocol:
            from evidence_protocol import EvidenceProtocol
            protocol=EvidenceProtocol(evidence,self.cfg['minimum_quote_characters'],self.cfg['max_claims'])
        payload={'model':self.cfg['model'],'messages':[{'role':'system','content':self.prompt},
                 {'role':'user','content':json.dumps({'question':query,'evidence':protocol.evidence if protocol else evidence,'retrieval_coverage':retrieved.get('coverage')},ensure_ascii=False)}],
                 **self.cfg['parameters']}
        if protocol:payload['response_format']=protocol.response_format()
        self.last_trace['model_input']=payload
        response=request(self.cfg['base_url'].rstrip('/')+'/chat/completions','POST',payload,
                         {'Authorization':'Bearer '+require(self.cfg['api_key_env'])},self.cfg['timeout_seconds'])
        self.last_trace['model_output']={'model':response.get('model'),'usage':response.get('usage',{}),'choices':[{'finish_reason':c.get('finish_reason'),'content':c.get('message',{}).get('content')} for c in response.get('choices',[])]}
        audit={**base,'generation_called':True,'provider_model':response.get('model'),
               'usage':response.get('usage',{})}
        choice={}
        try:
            choice=response['choices'][0]
            if choice.get('finish_reason')!='stop':raise PipelineError('Incomplete model output')
            obj=json.loads(choice['message']['content'])
            result=self.validate(protocol.resolve(obj) if protocol else obj,evidence)
        except (KeyError,IndexError,TypeError,json.JSONDecodeError,PipelineError) as error:
            reason='citation_quote_mismatch' if isinstance(error,PipelineError) and str(error)=='Citation quote is absent or too short' else 'invalid_model_output'
            self.last_trace['validation_error']=str(error)
            logging.getLogger(__name__).warning('answer_validation_failed category=%s detail=%s finish_reason=%s',reason,str(error),choice.get('finish_reason') if isinstance(choice,dict) else 'unknown')
            return {**audit,'status':'invalid_response','answer':self.cfg['messages']['invalid_response'],
                    'claims':[],'sources':[],'citation_validation':'failed','validation_failure_reason':reason}
        if result['status']=='insufficient':logging.getLogger(__name__).warning('answer_insufficient reason=model_decision evidence_count=%d',len(evidence))
        used=list(dict.fromkeys(ref['id'] for c in result['claims'] for ref in c['evidence']))
        sources=[{k:v for k,v in e.items() if k!='text'} for e in evidence if e['id'] in used]
        text=[]
        if result['status']!='answered':text.append(self.cfg['messages'][result['status']])
        for claim in result['claims']:
            ids=list(dict.fromkeys(ref['id'] for ref in claim['evidence']))
            text.append(claim['text']+' '+''.join(f'[{sid}]' for sid in ids))
        return {**audit,'answerability_reason':'model_insufficient' if result['status']=='insufficient' else 'supported_response','status':result['status'],'answer':'\n\n'.join(text),
                'claims':result['claims'],'sources':sources,'citation_validation':'passed'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--query',required=True)
    args=parser.parse_args();path=Path(args.config).resolve()
    try:
        read_env(path.parent.parent/'.env')
        result=AnswerService(Pipeline(path)).answer(args.query)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except PipelineError as e:parser.exit(1,str(e)+'\n')

if __name__=='__main__':main()
