"""Bind generated references to exact, request-local source paragraphs."""
import re
from rag_pipeline import PipelineError

def normalized(text):
    return re.sub(r'\s+','',text)

class EvidenceProtocol:
    def __init__(self, evidence, minimum, max_claims):
        self.refs={};self.evidence=[]
        for source in evidence:
            # Keep literal source spans; combine short headings with adjacent text.
            parts=re.split(r"\n\s*\n",source['text']);spans=[];pending=''
            for part in parts:
                pending=(pending+'\n\n'+part) if pending else part
                if len(normalized(pending))>=minimum:spans.append(pending);pending=''
            if pending and spans:spans[-1]+='\n\n'+pending
            elif pending:continue
            paragraphs=[]
            for i,text in enumerate(spans,1):
                key=f"{source['id']}.P{i}"
                self.refs[key]={'id':source['id'],'quote':text}
                paragraphs.append({'reference':key,'text':text})
            self.evidence.append({**{k:v for k,v in source.items() if k!='text'},'paragraphs':paragraphs})
        self.max_claims=max_claims

    def response_format(self):
        return {'type':'json_schema','json_schema':{'name':'grounded_answer','strict':True,'schema':{
            'type':'object','properties':{
                'status':{'type':'string','enum':['answered','insufficient','conflict']},
                'claims':{'type':'array','items':{'type':'object','properties':{
                    'text':{'type':'string'},
                    'evidence':{'type':'array','items':{'type':'string','enum':list(self.refs)}}
                },'required':['text','evidence'],'additionalProperties':False}}
            },'required':['status','claims'],'additionalProperties':False}}}

    def resolve(self,obj):
        if not isinstance(obj,dict) or set(obj)!={'status','claims'} or not isinstance(obj['claims'],list):
            raise PipelineError('Invalid answer object')
        claims=[]
        for claim in obj['claims']:
            if not isinstance(claim,dict) or set(claim)!={'text','evidence'} or not isinstance(claim['evidence'],list):
                raise PipelineError('Invalid reference claim')
            refs=[]
            for key in claim['evidence']:
                if not isinstance(key,str) or key not in self.refs:raise PipelineError('Unknown citation')
                if self.refs[key] not in refs:refs.append(dict(self.refs[key]))
            claims.append({**claim,'evidence':refs})
        return {**obj,'claims':claims}
