"""Offline planning and configured embedding, Supabase ingestion, hybrid retrieval CLI."""
import argparse
from retrieval_policy import validate_rules,plan_query,assemble_overviews
from collections import Counter
import hashlib
import http.client
import json
import logging
import socket
import ssl
import time
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

class PipelineError(ValueError):
    pass

class ServiceConnectionError(PipelineError):
    pass

def sha(data):
    return hashlib.sha256(data).hexdigest()

def safe_path(root, name):
    p=(root/name).resolve()
    if not p.is_relative_to(root):
        raise PipelineError('Configured path escapes project root')
    return p

def read_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            line=line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value=line.partition('=')
            if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*',key):
                raise PipelineError('Invalid environment file syntax')
            os.environ.setdefault(key,value.strip().strip('"\''))

def require(name):
    value=os.environ.get(name)
    if not value:
        raise PipelineError(f'Missing local environment variable: {name}')
    return value

def request(url, method='GET', payload=None, headers=None, timeout=60):
    # Only known read operations can be replayed, never model calls or database writes.
    read_only=method=='GET' or (method=='POST' and urllib.parse.urlsplit(url).path.endswith('/rest/v1/rpc/match_knowledge_chunks'))
    attempts=2 if read_only else 1
    for attempt in range(attempts):
        try:return _request_once(url,method,payload,headers,timeout)
        except ServiceConnectionError:
            if attempt+1==attempts:raise
            time.sleep(0.25)

def _request_once(url, method='GET', payload=None, headers=None, timeout=60):
    if urllib.parse.urlsplit(url).scheme!='https':
        raise PipelineError('HTTPS required for service connections')
    req=urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                               headers={'Content-Type':'application/json',**(headers or {})},method=method)
    path=urllib.parse.urlsplit(url).path
    service='向量模型' if path.endswith('/embeddings') else '回答模型' if path.endswith('/chat/completions') else '知识数据库' if '/rest/v1/' in path else '外部服务'
    started=time.monotonic()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            data=response.read()
        return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        if e.code == 400:
            try:
                code=json.loads(e.read()).get('error',{}).get('code')
            except Exception:code=None
            if code == 'Arrearage':raise PipelineError('模型平台当前计费状态不允许调用，请检查余额、欠费及该模型可用额度。') from None
        # Do not print response body, headers, keys, or source request content.
        raise PipelineError(f'Service returned HTTP {e.code}; check local credentials, region and schema') from None
    except (urllib.error.URLError,TimeoutError,http.client.HTTPException,OSError) as e:
        reason=e.reason if isinstance(e,urllib.error.URLError) else e
        if isinstance(reason,ssl.SSLCertVerificationError):category='certificate';detail='安全证书验证失败，请检查本机证书或网络代理。'
        elif isinstance(reason,ssl.SSLError):category='tls';detail='安全连接中断，请检查网络或代理后重试。'
        elif isinstance(reason,(TimeoutError,socket.timeout)):category='timeout';detail='连接或响应超时，请稍后重试。'
        elif isinstance(reason,socket.gaierror):category='dns';detail='域名解析失败，请检查网络或DNS设置。'
        elif isinstance(reason,http.client.IncompleteRead):category='incomplete_response';detail='响应传输中断，请稍后重试。'
        else:category='connection';detail='网络连接失败，请检查网络或代理后重试。'
        # Never log endpoint URLs, credentials, payloads, provider bodies or raw exceptions.
        logging.getLogger(__name__).warning('service_connection_failure stage=%s category=%s exception=%s elapsed=%.2f',service,category,type(reason).__name__,time.monotonic()-started)
        raise ServiceConnectionError(service+'：'+detail) from None

def validate_vector(v, dimensions):
    if not isinstance(v,list) or len(v)!=dimensions or any(type(x) not in (int,float) or not math.isfinite(x) for x in v) or not any(v):
        raise PipelineError('Invalid embedding dimensions, numeric values or zero vector')
    return v

def tokens(text):
    # Chinese character bigrams + Latin/numeric terms, no English FTS assumption.
    result=re.findall(r'[a-z0-9]+(?:\.[0-9]+)?',text.lower())
    for phrase in re.findall(r'[\u4e00-\u9fff]+',text):
        result.extend(phrase[i:i+2] for i in range(len(phrase)-1))
        if len(phrase)==1:result.append(phrase)
    return result

def bm25(query, rows):
    q=set(tokens(query));docs=[Counter(tokens(r['embedding_text'])) for r in rows]
    if not q or not rows:return []
    avg=sum(sum(d.values()) for d in docs)/len(docs) or 1
    df={t:sum(t in d for d in docs) for t in q}
    scores=[]
    for row,d in zip(rows,docs):
        score=0
        for t in q:
            f=d[t]
            if f:
                idf=math.log(1+(len(docs)-df[t]+0.5)/(df[t]+0.5))
                score+=idf*f*2.2/(f+1.2*(0.25+0.75*sum(d.values())/avg))
        if score>0:scores.append((row,score))
    return sorted(scores,key=lambda pair:(-pair[1],pair[0]['chunk_id']))

def fuse(vector_rows, keyword_rows, cfg):
    scores={};records={}
    for ranking in (vector_rows,keyword_rows):
        seen=set()
        for rank,row in enumerate(ranking,1):
            cid=row['chunk_id']
            if cid in seen:continue
            seen.add(cid);records[cid]=row
            scores[cid]=scores.get(cid,0)+1/(cfg['rrf_constant']+rank)
    ordered=sorted(scores,key=lambda cid:(-scores[cid],cid))[:cfg['rerank_candidate_limit']]
    return [{**records[cid],'rrf_score':scores[cid]} for cid in ordered]

class Pipeline:
    def __init__(self, config, allow_empty=False):
        self.path=Path(config).resolve();self.root=self.path.parent.parent
        self.cfg=json.loads(self.path.read_text());c=self.cfg;e=c['embedding'];r=c['retrieval']
        try:validate_rules(r.get('query_rules',[]))
        except ValueError as error:raise PipelineError(str(error)) from None
        if type(r.get('trace_enabled',False)) is not bool or type(r.get('trace_retention_count',20)) is not int or not 1<=r.get('trace_retention_count',20)<=100:raise PipelineError('调试记录配置无效。')
        for k in ['dimensions','batch_size']:
            if type(e[k]) is not int or e[k]<1:raise PipelineError('Positive integer embedding settings required')
        for k in ['vector_top_k','keyword_top_k','rerank_candidate_limit','context_top_k']:
            if type(r[k]) is not int or not 1<=r[k]<=100:raise PipelineError('Top-K must be integer in [1,100]')
        if r['rerank_enabled']:raise PipelineError('Reranker adapter not implemented; disable rerank for this version')
        if r['rrf_constant']<=0 or r['max_context_characters']<1:raise PipelineError('Invalid retrieval budget')
        if not -1<=r['minimum_cosine_similarity']<=1:raise PipelineError('Invalid cosine threshold')
        if not c['tenant_id']:raise PipelineError('Trusted deployment tenant ID required')
        raw=safe_path(self.root,c['chunks_path']).read_bytes()
        report=json.loads(safe_path(self.root,c['chunk_report_path']).read_text())
        if sha(raw)!=report['chunks_sha256']:raise PipelineError('Chunk output hash mismatch; rebuild or verify source')
        all_rows=[json.loads(x) for x in raw.decode().splitlines()]
        self.context={x['chunk_id']:x for x in all_rows}
        self.rows=[x for x in all_rows if x.get('ingestion_allowed') is True and x.get('retrieval_eligible') is True]
        if (not self.rows and not allow_empty) or len({x['chunk_id'] for x in self.rows})!=len(self.rows):raise PipelineError('Empty or duplicate chunks')
        self.profile={'base_url':e['base_url'].rstrip('/'),'model':e['model'],'dimensions':e['dimensions']}
        self.index_id=sha(json.dumps({'profile':self.profile,'chunks_sha256':sha(raw)},sort_keys=True).encode())
        self.cache_path=safe_path(self.root,c['vector_cache_path'])
        source_paths=[safe_path(self.root,c[k]) for k in ('chunks_path','chunk_report_path')]
        if self.cache_path in source_paths:raise PipelineError('Cache cannot overwrite source')

    def plan(self):
        return {'eligible_chunks':len(self.rows),'dimensions':self.cfg['embedding']['dimensions'],
                'embedding_batches':math.ceil(len(self.rows)/self.cfg['embedding']['batch_size']),
                'index_id':self.index_id,'model':self.profile['model'],
                'input_characters':sum(len(r['embedding_text']) for r in self.rows),
                'network_called':False,'note':'Character count is not a tokenizer or a cost estimate.'}

    def embed_texts(self,texts):
        e=self.cfg['embedding'];key=require(e['api_key_env'])
        resp=request(e['base_url'].rstrip('/')+'/embeddings','POST',
                     {'model':e['model'],'input':texts,'dimensions':e['dimensions'],'encoding_format':'float'},
                     {'Authorization':'Bearer '+key},e['timeout_seconds'])
        data=resp.get('data',[])
        if len(data)!=len(texts) or sorted(x.get('index',-1) for x in data)!=list(range(len(texts))):
            raise PipelineError('Embedding response count/index mismatch')
        return [validate_vector(x['embedding'],e['dimensions']) for x in sorted(data,key=lambda x:x['index'])]

    def cache(self):
        if not self.cache_path.exists():return {'index_id':self.index_id,'profile':self.profile,'vectors':{}}
        obj=json.loads(self.cache_path.read_text())
        if obj.get('index_id')!=self.index_id or obj.get('profile')!=self.profile:
            raise PipelineError('Embedding cache belongs to a different model/source version; use a new cache path')
        for cid,item in obj['vectors'].items():validate_vector(item['vector'],self.profile['dimensions'])
        return obj

    def save_cache(self,obj):
        self.cache_path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',dir=self.cache_path.parent,delete=False) as f:
            json.dump(obj,f,ensure_ascii=False);tmp=f.name
        os.replace(tmp,self.cache_path)

    def embed(self,progress=None):
        obj=self.cache();pending=[]
        for row in self.rows:
            item=obj['vectors'].get(row['chunk_id'])
            if item and item.get('input_sha256')!=sha(row['embedding_text'].encode()):raise PipelineError('Cache input hash mismatch')
            if not item:pending.append(row)
        size=self.cfg['embedding']['batch_size']
        for i in range(0,len(pending),size):
            batch=pending[i:i+size];vectors=self.embed_texts([x['embedding_text'] for x in batch])
            for row,vector in zip(batch,vectors):obj['vectors'][row['chunk_id']]={'input_sha256':sha(row['embedding_text'].encode()),'vector':vector}
            self.save_cache(obj)
            if progress:progress('embedding',len(obj['vectors']),len(self.rows))
        if progress:progress('embedding',len(obj['vectors']),len(self.rows))
        return {'cached_vectors':len(obj['vectors']),'new_vectors':len(pending),'index_id':self.index_id}

    def db(self,path,method='GET',payload=None,prefer=None):
        d=self.cfg['database'];url=require(d['url_env']).rstrip('/')
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise PipelineError('Supabase URL must be an HTTPS API origin without a page path or credentials')
        key=require(d['service_key_env'])
        h={'apikey':key}
        if not key.startswith('sb_secret_'):h['Authorization']='Bearer '+key
        if prefer:h['Prefer']=prefer
        return request(url+'/rest/v1/'+path,method,payload,h,d['timeout_seconds'])

    def upload(self,progress=None):
        obj=self.cache()
        for row in self.rows:
            item=obj['vectors'].get(row['chunk_id'])
            if not item or item.get('input_sha256')!=sha(row['embedding_text'].encode()):raise PipelineError('Complete matching embedding cache required before upload')
        # Version registration validates profile and never downgrades a ready index.
        state=self.db('rpc/register_knowledge_index','POST',{'p_tenant':self.cfg['tenant_id'],'p_index':self.index_id,
                'p_profile':self.profile,'p_expected':len(self.rows)})
        if state=='ready':
            if progress:progress('upload',len(self.rows),len(self.rows))
            return {'state':'ready','index_id':self.index_id,'already_uploaded':True,'confirmed_rows':len(self.rows),'written_rows':0}
        size=self.cfg['database']['batch_size']
        if type(size) is not int or size<1:raise PipelineError('Invalid database batch size')
        for i in range(0,len(self.rows),size):
            batch=[{'tenant_id':self.cfg['tenant_id'],'index_id':self.index_id,'chunk_id':row['chunk_id'],
                    'payload':row,'embedding':obj['vectors'][row['chunk_id']]['vector']} for row in self.rows[i:i+size]]
            self.db('knowledge_chunks?on_conflict=tenant_id,index_id,chunk_id','POST',batch,'resolution=merge-duplicates,return=minimal')
            if progress:progress('upload',min(i+size,len(self.rows)),len(self.rows))
        if progress:progress('verification',0,None)
        result=self.db('rpc/finalize_knowledge_index','POST',{'p_tenant':self.cfg['tenant_id'],'p_index':self.index_id})
        if isinstance(result,dict) and result.get('state')=='ready':result={**result,'confirmed_rows':len(self.rows),'written_rows':len(self.rows)}
        return result

    def local_vector_search(self,vector):
        """Exact cosine search over this config's fully verified current cache only."""
        cache=self.cache();scored=[];norm=math.sqrt(sum(x*x for x in vector))
        for row in self.rows:
            item=cache['vectors'].get(row['chunk_id'])
            if not item or item.get('input_sha256')!=sha(row['embedding_text'].encode()):
                raise PipelineError('云端检索不可用，且本地当前版本向量不完整或校验失败。')
            v=item['vector'];similarity=sum(a*b for a,b in zip(vector,v))/(norm*math.sqrt(sum(x*x for x in v)))
            if similarity>=self.cfg['retrieval']['minimum_cosine_similarity']:
                scored.append({'chunk_id':row['chunk_id'],'similarity':similarity})
        return sorted(scored,key=lambda x:(-x['similarity'],x['chunk_id']))[:self.cfg['retrieval']['vector_top_k']]

    def search(self,query):
        if not query.strip():raise PipelineError('Empty question')
        blocked=getattr(self,'blocked_documents',set())
        eligible=[row for row in self.rows if row['document_id'] not in blocked]
        r=self.cfg['retrieval'];plan=plan_query(query,r.get('query_rules',[]));vector=self.embed_texts([plan['search_query']])[0]
        backend='supabase'
        try:
            candidates=self.db('rpc/match_knowledge_chunks','POST',{'p_tenant':self.cfg['tenant_id'],'p_index':self.index_id,
                              'p_vector':vector,'p_limit':r['vector_top_k'],'p_threshold':r['minimum_cosine_similarity']})
        except ServiceConnectionError:
            if not self.cfg['database'].get('local_read_fallback',False):raise
            candidates=self.local_vector_search(vector);backend='local_vector_cache'
            logging.getLogger(__name__).warning('retrieval_fallback backend=local_vector_cache index=%s',self.index_id)
        ranked=[]
        for x in candidates or []:
            row=self.context.get(x['chunk_id'])
            if row and row['document_id'] not in blocked and row.get('retrieval_eligible'):ranked.append(row)
        # Keyword ranking uses the identical verified local index snapshot; no unrelated tenant data is fetched.
        lexical=bm25(plan['search_query'],eligible)
        keyword=[row for row,score in lexical[:r['keyword_top_k']]]
        reserved=[]
        for group in r.get('source_groups',[]):
            if any(term in query for term in group['triggers']):
                for doc_id in group['document_ids']:
                    best=next((row for row,score in lexical if row['document_id']==doc_id),None)
                    if best is not None and best['chunk_id'] not in {x['chunk_id'] for x in reserved}:reserved.append(best)
        fused=fuse(ranked,keyword,r)
        seen_reserved={x['chunk_id'] for x in reserved}
        fused=reserved+[x for x in fused if x['chunk_id'] not in seen_reserved]
        fused=fused[:r['rerank_candidate_limit']]
        def summary(row,rank,score=None):
            return {'rank':rank,'chunk_id':row['chunk_id'],'document_id':row['document_id'],'locator':row['locator'],**({'score':score} if score is not None else {})}
        trace={'limits':{k:r[k] for k in ('vector_top_k','keyword_top_k','rerank_candidate_limit','context_top_k','max_context_characters')},'query_plan':plan,'vector_candidates':[summary(row,i+1,next((c.get('similarity') for c in candidates if c.get('chunk_id')==row['chunk_id']),None)) for i,row in enumerate(ranked)],
               'keyword_candidates':[summary(row,i+1,score) for i,(row,score) in enumerate(lexical[:r['keyword_top_k']])],
               'fused_candidates':[summary(row,i+1,row.get('rrf_score')) for i,row in enumerate(fused)],'selection':[]}
        trace['original_keyword_candidates']=[summary(row,i+1,score) for i,(row,score) in enumerate(bm25(query,eligible)[:r['keyword_top_k']])] if plan['search_query']!=query else trace['keyword_candidates']
        in_fused={row['chunk_id'] for row in fused}
        trace['fusion_dropped']=[summary(row,i+1) for i,row in enumerate({row['chunk_id']:row for row in ranked+keyword}.values()) if row['chunk_id'] not in in_fused]
        packs,missing=assemble_overviews(eligible,plan['overview_groups'])
        for row in packs:self.context[row['chunk_id']]=row
        selection=packs if plan['overview_groups'] else fused
        selected=[];used=0
        for row in selection:
            context=[self.context[cid]['body'] for cid in row.get('context_chunk_ids',[]) if self.context[cid]['document_id'] not in blocked]
            text='\n\n'.join([*context,row['embedding_text']])
            reason='selected'
            if len(selected)>=r['context_top_k']:reason='context_top_k'
            elif used+len(text)>r['max_context_characters']:reason='context_budget'
            trace['selection'].append({**summary(row,len(trace['selection'])+1),'characters':len(text),'decision':reason,'source_chunk_ids':row.get('source_chunk_ids',[row['chunk_id']])})
            if reason!='selected':continue
            used+=len(text)
            selected.append({'chunk_id':row['chunk_id'],'document_id':row['document_id'],'locator':row['locator'],
                             'source_url':row['source_url'],'validity_note':row['validity_note'],'usage_note':row.get('usage_note'),
                             'source_chunk_ids':row.get('source_chunk_ids',[row['chunk_id']]),
                             'context_text':text,'rrf_score':row.get('rrf_score',0.0)})
        if plan['overview_groups']:
            trace['selection'] += [{**summary(row,i+1),'decision':'overview_route'} for i,row in enumerate(fused)]
        coverage={'mode':'overview' if plan['overview_groups'] else 'specific','requested_groups':plan['overview_groups'],
                  'missing_groups':missing,'selected_groups':len(selected) if packs else 0,
                  'complete':not missing and len(selected)==len(plan['overview_groups']) if plan['overview_groups'] else None}
        return {'index_id':self.index_id,'retrieval_backend':backend,'evidence':selected,'context_characters':used,
                'coverage':coverage,'trace':trace,
                'answer_generated':False,'note':'Retrieval evidence only; no relevance guarantee or conflict adjudication.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True)
    p.add_argument('command',choices=['plan','embed','upload','search']);p.add_argument('--query')
    args=p.parse_args();cfg=Path(args.config).resolve()
    try:
        read_env(cfg.parent.parent/'.env')
        pipeline=Pipeline(cfg)
        if args.command=='search':
            if not args.query:p.error('--query required')
            result=pipeline.search(args.query)
        else:result=getattr(pipeline,args.command)()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except PipelineError as e:p.exit(1,str(e)+'\n')

if __name__=='__main__':main()
