"""Validated server-side settings, versioned prompts, no secret values in exports."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit
from rag_pipeline import Pipeline,PipelineError,safe_path
from answer_service import AnswerService

LOCK=threading.RLock()
SECRETS=('DASHSCOPE_API_KEY','SUPABASE_URL','SUPABASE_SECRET_KEY')
TRUSTED_HOSTS={'dashscope.aliyuncs.com','dashscope-intl.aliyuncs.com','open.bigmodel.cn'}

def atomic(path,text,private=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(text)
        if private:os.chmod(name,0o600)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def endpoint(url):
    u=urlsplit(url)
    allowed=TRUSTED_HOSTS | set(filter(None,os.environ.get('APP_TRUSTED_MODEL_HOSTS','').split(',')))
    if u.scheme!='https' or u.username or u.password or u.query or u.fragment or u.port not in (None,443) or (u.hostname not in allowed and not (u.hostname or '').endswith('.maas.aliyuncs.com')):
        raise PipelineError('模型接入地址必须使用受信任的HTTPS服务域名。')

def number(value,lo,hi,integer=False):
    if type(value) not in ((int,) if integer else (int,float)) or not math.isfinite(value) or not lo<=value<=hi:
        raise PipelineError(f'数值必须在{lo}至{hi}之间。')

class SettingsStore:
    def __init__(self,config):
        self.path=Path(config).resolve();self.root=self.path.parent.parent
        self.registry=self.root/'config/knowledge-profiles.json'
    def read(self):return json.loads(self.path.read_text())
    def revision(self):
        c=self.read();prompt=safe_path(self.root,c['answer']['prompt_path']).read_text()
        return hashlib.sha256((json.dumps(c,sort_keys=True)+prompt).encode()).hexdigest()
    def view(self):
        c=self.read();c.setdefault('business',json.loads((self.root/'config/business-defaults.json').read_text()));p=Pipeline(self.path,allow_empty=True)
        return {'config':c,'prompt':safe_path(self.root,c['answer']['prompt_path']).read_text(),
                'revision':self.revision(),'profiles':json.loads(self.registry.read_text()),
                'credentials':{k:bool(os.environ.get(k)) for k in SECRETS},
                'index_id':p.index_id}
    def validate(self,c,old,prompt):
        if set(c)-{'business'}!=set(old)-{'business'}:raise PipelineError('不允许添加未知顶层配置。')
        business=c.get('business',{})
        if not isinstance(business,dict) or set(business)-{'scope','audience','tone','handoff'} or any(not isinstance(v,str) or len(v)>2000 for v in business.values()):raise PipelineError('业务设置格式无效。')
        # Prevent arbitrary paths, tenant switching and credential-variable reads from the browser.
        for k in ('tenant_id','vector_cache_path'):
            if c[k]!=old[k]:raise PipelineError('部署标识及缓存路径由服务端管理。')
        for group,key in [('embedding','api_key_env'),('database','url_env'),('database','service_key_env'),('answer','api_key_env')]:
            if c[group][key]!=old[group][key]:raise PipelineError('凭据变量名由服务端管理。')
        if c['answer']['prompt_path']!=old['answer']['prompt_path']:raise PipelineError('模板路径由服务端管理。')
        if c['retrieval'].get('rerank_enabled') is not False:raise PipelineError('首版未启用重排序。')
        profiles=json.loads(self.registry.read_text());profile=next((p for p in profiles if p['id']==c['web']['knowledge_profile']),None)
        if profile is None:raise PipelineError('未知知识版本。')
        for key in ('chunks_path','chunk_report_path'):c[key]=profile[key]
        endpoint(c['embedding']['base_url']);endpoint(c['answer']['base_url'])
        number(c['embedding']['dimensions'],64,4096,True);number(c['embedding']['batch_size'],1,20,True)
        for section in ('embedding','answer','database'):number(c[section]['timeout_seconds'],5,180,True)
        number(c['database']['batch_size'],1,100,True)
        if type(c['database'].get('local_read_fallback',False)) is not bool:raise PipelineError('本地备用检索开关必须是布尔值。')
        r=c['retrieval']
        for k in ('vector_top_k','keyword_top_k','rerank_candidate_limit','context_top_k'):number(r[k],1,100,True)
        number(r['minimum_cosine_similarity'],-1,1);number(r['max_context_characters'],500,24000,True);number(r['rrf_constant'],1,200,True)
        if len(r.get('source_groups',[]))>20:raise PipelineError('来源组数量过多。')
        for g in r.get('source_groups',[]):
            if set(g)!={'triggers','document_ids'} or not all(isinstance(v,list) and v and all(isinstance(x,str) and 0<len(x)<200 for x in v) for v in g.values()):raise PipelineError('来源组格式无效。')
        a=c['answer'];params=a['parameters']
        if set(params)-{'temperature','max_tokens','enable_thinking','response_format','top_p','thinking_budget'}:raise PipelineError('不支持的模型参数。')
        number(params['temperature'],0,2);number(params['max_tokens'],128,8192,True)
        if 'top_p' in params:number(params['top_p'],0.01,1)
        if 'thinking_budget' in params:number(params['thinking_budget'],16,32768,True)
        if type(params['enable_thinking']) is not bool or params['response_format']!={'type':'json_object'}:raise PipelineError('当前需要JSON响应模式。')
        for k in ('max_question_characters','minimum_quote_characters','max_claims'):number(a[k],1,10000,True)
        if len(prompt)<80 or len(prompt)>24000:raise PipelineError('业务Prompt长度须为80至24000字符。')
        for k in ('model',):
            if not isinstance(a[k],str) or not 1<=len(a[k])<=200 or not isinstance(c['embedding'][k],str) or not 1<=len(c['embedding'][k])<=200:raise PipelineError('模型名无效。')
        for k in ('assistant_name','company_name','description'):
            if not isinstance(c['web'][k],str) or not 1<=len(c['web'][k])<=300:raise PipelineError('企业信息长度无效。')
        if not __import__('re').fullmatch(r'#[0-9a-fA-F]{6}',c['web'].get('accent','#2479b9')) or c['web'].get('theme','clean') not in ('clean','soft','night'):
            raise PipelineError('主题色或风格无效。')
        for k,limit in [('welcome_title',100),('welcome_subtitle',200)]:
            if not isinstance(c['web'].get(k,''),str) or len(c['web'].get(k,''))>limit:raise PipelineError('欢迎文案长度无效。')
        if any(c['web'].get(k,'')!=old['web'].get(k,'') for k in ('logo_asset','logo_version')):raise PipelineError('请通过Logo上传入口设置。')
        ex=c['web']['examples']
        if not isinstance(ex,list) or len(ex)>8 or not all(isinstance(x,str) and len(x)<=300 for x in ex):raise PipelineError('示例问题无效。')
        number(c['web']['request_limit_per_minute'],1,60,True)
        for key in ('insufficient','conflict','invalid_response'):
            if not isinstance(a['messages'][key],str) or not 1<=len(a['messages'][key])<=1000:raise PipelineError('状态提示无效。')
        return c
    def save(self,data):
        with LOCK:
            if data.get('revision')!=self.revision():raise PipelineError('配置已变化，请刷新后再保存。')
            old=self.read();c=copy.deepcopy(data['config']);prompt=data['prompt'];self.validate(c,old,prompt)
            if c==old and prompt==safe_path(self.root,old['answer']['prompt_path']).read_text():return self.view()
            ph=hashlib.sha256(prompt.encode()).hexdigest()
            c['answer']['prompt_path']=f'prompts/versions/{ph}.txt'
            # Test all pipeline/answer validations against the candidate without touching live settings.
            tmp=self.path.parent/'candidate-validation.json'
            atomic(safe_path(self.root,c['answer']['prompt_path']),prompt)
            try:
                atomic(tmp,json.dumps(c,ensure_ascii=False,indent=2));candidate=Pipeline(tmp,allow_empty=True);AnswerService(candidate)
                if candidate.index_id!=Pipeline(self.path,allow_empty=True).index_id:
                    c['vector_cache_path']=f'local-data/indexes/{candidate.index_id}.json'
            finally:
                tmp.unlink(missing_ok=True)
            history=self.root/'local-data/settings-history'
            atomic(history/(self.revision()+'.json'),json.dumps(old,ensure_ascii=False,indent=2))
            atomic(self.path,json.dumps(c,ensure_ascii=False,indent=2))
            return self.view()
    def save_credentials(self,values):
        if set(values)-set(SECRETS):raise PipelineError('未知凭据字段。')
        selected={k:v.strip() for k,v in values.items() if isinstance(v,str) and v.strip()}
        if any('\n' in v or '\r' in v or len(v)>5000 for v in selected.values()):raise PipelineError('凭据格式无效。')
        if 'SUPABASE_URL' in selected:
            u=urlsplit(selected['SUPABASE_URL'])
            if u.scheme!='https' or u.username or u.password or u.port not in (None,443) or not (u.hostname or '').endswith('.supabase.co') or u.path.strip('/') or u.query or u.fragment:raise PipelineError('请填写Supabase项目API域名，而不是控制台页面地址。')
        if 'SUPABASE_SECRET_KEY' in selected and not (selected['SUPABASE_SECRET_KEY'].startswith('sb_secret_') or selected['SUPABASE_SECRET_KEY'].startswith('eyJ')):raise PipelineError('需要服务端Secret Key。')
        with LOCK:
            path=self.root/'.env';lines=path.read_text().splitlines() if path.exists() else []
            kept=[x for x in lines if x.partition('=')[0] not in selected]
            atomic(path,'\n'.join(kept+[f'{k}={v}' for k,v in selected.items()])+'\n',private=True)
            os.environ.update(selected)
        return {k:bool(os.environ.get(k)) for k in SECRETS}
