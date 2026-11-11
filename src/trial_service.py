"""Server-only anonymous ownership, durable quotas and private workspace checkpointing."""
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import urllib.request
import urllib.error
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from rag_pipeline import Pipeline,PipelineError,require,safe_path
from settings_store import atomic,LOCK

class TrialError(PipelineError):
    def __init__(self,message,status=403):super().__init__(message);self.status=status

def now():return datetime.now(timezone.utc)

class CloudWorkspace:
    """One Render worker; DB lease fences rolling deploys. Immutable private blobs + atomic manifest.

    A completed request is acknowledged only after its checkpoint is committed. Database
    outage fails closed. Paid quota reservations are independent of file checkpoints.
    """
    def __init__(self,config):
        self.seed=Path(config).resolve();self.root=self.seed.parent.parent
        self.limits=json.loads((self.root/'config/trial-limits.json').read_text())
        self.pipeline=Pipeline(config,allow_empty=True)
        self.workspace=self.limits['workspace'];self.token=secrets.token_hex(24)
        self.lock=threading.RLock();self.building=False;self.manifest={};self.revision=-1
        self.leased=False;self.lease_failed=False;self.needs_restore=False;self.restore()
    def rpc(self,name,**kwargs):return self.pipeline.db('rpc/knowflow_trial_'+name,'POST',{'p_workspace':self.workspace,**{'p_'+k:v for k,v in kwargs.items()}})
    def acquire(self):
        if not self.rpc('lock',token=self.token,seconds=300):raise TrialError('工作空间正在保存或构建，请稍后重试。',409)
        self.leased=True;self.lease_failed=False
    def heartbeat(self,stop):
        while not stop.wait(30):
            try:
                if not self.rpc('lock',token=self.token,seconds=300):self.lease_failed=True;return
            except Exception:self.lease_failed=True;return
    def release(self):
        if self.leased:self.rpc('unlock',token=self.token);self.leased=False
    def blob(self,digest,data=None):
        d=self.pipeline.cfg['database'];key=require(d['service_key_env'])
        headers={'apikey':key}
        if not key.startswith('sb_secret_'):headers['Authorization']='Bearer '+key
        url=require(d['url_env']).rstrip('/')+'/storage/v1/object/'+self.limits['bucket']+'/'+self.workspace+'/'+digest
        if data is not None:headers.update({'Content-Type':'application/octet-stream','x-upsert':'true'})
        request=urllib.request.Request(url,data=data,headers=headers,method='POST' if data is not None else 'GET')
        try:
            with urllib.request.urlopen(request,timeout=60) as response:return response.read()
        except (urllib.error.URLError,TimeoutError) as error:
            raise TrialError('私有资料保存服务暂不可用，请稍后重试。',503) from error
    def allowed(self,rel):
        p=Path(rel)
        return not p.is_absolute() and '..' not in p.parts and not any(x.startswith('.') for x in p.parts) and (p.parts[:1]==('local-data',) or (p.parts[:1]==('prompts',) and p.suffix=='.txt') or rel==str(self.seed.relative_to(self.root))) and 'traces' not in p.parts
    def restore(self,force=False):
        force=force or self.needs_restore
        rows=self.pipeline.db('knowflow_trial_workspace?id=eq.'+self.workspace+'&select=manifest,revision')
        if not rows:return
        state=rows[0]
        if state['revision']==self.revision and not force:return
        manifest=state['manifest']
        staged=[]
        for rel,info in manifest.items():
            if not self.allowed(rel) or not re.fullmatch(r'[0-9a-f]{64}',info['sha']):raise TrialError('云端资料清单校验失败。',503)
            p=safe_path(self.root,rel)
            if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=info['sha']:
                raw=gzip.decompress(self.blob(info['sha']))
                if len(raw)!=info['bytes'] or hashlib.sha256(raw).hexdigest()!=info['sha']:raise TrialError('云端文件完整性校验失败。',503)
                staged.append((p,raw))
        # Remove abandoned cache files only on rollback, not durable history.
        if force:
            for folder in ('local-data','prompts'):
                for p in (self.root/folder).rglob('*'):
                    rel=str(p.relative_to(self.root))
                    if p.is_file() and self.allowed(rel) and rel not in manifest:p.unlink()
        # Do not expose a partly restored catalog.
        with LOCK:
            for p,raw in sorted(staged,key=lambda x:x[0].name=='assistant-registry.json'):
                p.parent.mkdir(parents=True,exist_ok=True);atomic(p,raw,private=True)
        self.manifest=manifest;self.revision=state['revision'];self.needs_restore=False
    def persist(self):
        if not self.leased or self.lease_failed:raise TrialError('工作空间未获得保存权限。',503)
        if not self.rpc('lock',token=self.token,seconds=300):raise TrialError('保存权限已过期，请刷新重试。',503)
        files=[self.seed]
        for folder in ('local-data','prompts'):
            files.extend(p for p in (self.root/folder).rglob('*') if p.is_file() and not p.is_symlink())
        manifest={}
        with LOCK:
            payloads=[(str(p.relative_to(self.root)),p.read_bytes()) for p in files if self.allowed(str(p.relative_to(self.root)))]
        known={v['sha'] for v in self.manifest.values()}
        for rel,raw in payloads:
            digest=hashlib.sha256(raw).hexdigest();manifest[rel]={'sha':digest,'bytes':len(raw)}
            if digest not in known:self.blob(digest,gzip.compress(raw,mtime=0));known.add(digest)
        if manifest!=self.manifest:
            self.revision=self.rpc('commit',token=self.token,manifest=manifest);self.manifest=manifest
    def owner(self,client):return hmac.new(require('APP_SESSION_SECRET').encode(),('trial:'+client).encode(),hashlib.sha256).hexdigest()
    def expired(self,record):return bool(record.get('expires_at') and now()>=datetime.fromisoformat(record['expires_at']))
    def owns(self,record,client):return bool(record.get('owner') and hmac.compare_digest(record['owner'],self.owner(client)))
    def check_create(self,catalog,client):
        records=catalog.data()['assistants']
        if any(self.owns(r,client) for r in records):raise TrialError('每个访客只能创建一个助手；请继续配置或恢复已有助手。',409)
        if sum(bool(r.get('owner')) and not r.get('deleted_at') and not self.expired(r) for r in records)>=self.limits['active_assistants']:raise TrialError('试用名额暂满，仍可体验样例问答。',429)
    def attach(self,catalog,aid,client):
        return catalog.update(aid,owner=self.owner(client),expires_at=(now()+timedelta(days=self.limits['days'])).isoformat(),uploaded_bytes=0)
    def consume(self,client,kind,amount=1):
        day=now().astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat();subject=self.owner(client);checks=[]
        def add(s,p,k,n,limit):checks.append({'subject':s,'period':p,'kind':k,'amount':n,'limit':limit})
        if kind=='question':
            add(subject,day,kind,amount,self.limits['questions_daily']);add('global',day,kind,amount,self.limits['global_questions_daily'])
            add(subject,str(int(now().timestamp())//60),'question_minute',amount,self.limits['questions_minute'])
        elif kind=='build':add(subject,day,kind,amount,self.limits['builds_daily'])
        elif kind=='vectors':add('global',day,kind,amount,self.limits['global_vectors_daily'])
        if not self.rpc('consume',checks=checks):raise TrialError('已达到试用额度，请稍后或明天再试。',429)
    def upload_check(self,catalog,aid,content,replacement=None):
        from document_store import DocumentStore
        r=catalog.get(aid);docs=DocumentStore(catalog,aid).all();limit=self.limits
        if len(content)>limit['file_bytes']:raise TrialError('每份资料最多5MB。',413)
        if r.get('uploaded_bytes',0)+len(content)>limit['total_upload_bytes']:raise TrialError('已达到累计20MB上传额度。',429)
        if replacement is None and sum(not d.get('removed') for d in docs)>=limit['files']:raise TrialError('每个助手最多10份资料，请删除或更新已有文件。',429)
    def build_check(self,pipeline):
        if len(pipeline.rows)>self.limits['chunks']:raise TrialError('切片超过500个，请减少资料后重新构建。',429)
        if any(r['document_id'].startswith('upload-') and len(r['body'])>self.limits['chunk_characters'] for r in pipeline.rows):raise TrialError('存在超过单片长度限制的内容，请调整资料。',429)

    def ready_index(self,pipeline):
        from urllib.parse import urlencode
        filters=urlencode({'tenant_id':'eq.'+pipeline.cfg['tenant_id'],'index_id':'eq.'+pipeline.index_id})
        rows=pipeline.db('knowledge_indexes?select=state,expected_count,profile&'+filters)
        if not rows or rows[0]['state']!='ready':return False
        row=rows[0]
        if row['expected_count']!=len(pipeline.rows) or row['profile']!=pipeline.profile:raise TrialError('已有索引配置不兼容，请联系管理员。',409)
        ids=pipeline.db('knowledge_chunks?select=chunk_id&'+filters)
        if {r['chunk_id'] for r in ids}!={r['chunk_id'] for r in pipeline.rows}:raise TrialError('已有索引片段不完整，请联系管理员。',409)
        return True
    def reuse_vectors(self,pipeline,catalog,record):
        from urllib.parse import urlencode
        from rag_pipeline import validate_vector,sha
        if not record.get('active_path'):return pipeline.cache()
        active=Pipeline(catalog.path(record,active=True))
        cache=pipeline.cache()
        if active.profile!=pipeline.profile:return cache
        filters=urlencode({'tenant_id':'eq.'+active.cfg['tenant_id'],'index_id':'eq.'+active.index_id})
        old=active.db('knowledge_chunks?select=chunk_id,payload,embedding&'+filters)
        current={r['chunk_id']:r for r in pipeline.rows}
        for r in old:
            cid=r['chunk_id'];body=r['payload'].get('embedding_text')
            if cid not in current or body!=current[cid]['embedding_text']:continue
            vector=json.loads(r['embedding']) if isinstance(r['embedding'],str) else r['embedding']
            validate_vector(vector,pipeline.profile['dimensions'])
            cache['vectors'][cid]={'input_sha256':sha(body.encode()),'vector':vector}
        pipeline.save_cache(cache);return cache
