"""Assistant lifecycle within one independently deployed enterprise workspace."""
import copy,json,re,secrets,shutil
from pathlib import Path
from datetime import datetime,timezone
from rag_pipeline import Pipeline,PipelineError,safe_path
from settings_store import atomic,LOCK,SettingsStore

def read(path):return json.loads(Path(path).read_text())
def write(path,obj):atomic(Path(path),json.dumps(obj,ensure_ascii=False,indent=2))

def clone_config(source,target,tenant=None):
    source=Path(source);source_root=source.parent.parent;c=read(source)
    target=Path(target);target.mkdir(parents=True,exist_ok=True)
    # Copy only knowledge inputs, prompt files, registry, optional vector cache. No credentials or conversations.
    prefixes=set(Path(c[k]).parts[0] for k in ('chunks_path','chunk_report_path'))
    prefixes.add(Path(c['answer']['prompt_path']).parts[0])
    for prefix in prefixes:
        p=safe_path(source_root,prefix)
        if p.is_dir():shutil.copytree(p,target/prefix,dirs_exist_ok=True)
        else:shutil.copy2(p,target/prefix)
    registry=source_root/'config/knowledge-profiles.json'
    write(target/'config/knowledge-profiles.json',read(registry))
    for name in ('business-defaults.json','model-options.json'):
        source_file=source_root/'config'/name
        if source_file.exists():write(target/'config'/name,read(source_file))
    if tenant:c['tenant_id']=tenant
    cache=safe_path(source_root,c['vector_cache_path'])
    if cache.exists():
        cp=safe_path(target,c['vector_cache_path']);cp.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(cache,cp)
    write(target/'config/app.json',c)
    return target/'config/app.json'

class AssistantCatalog:
    def __init__(self,config):
        self.seed=Path(config).resolve();self.root=self.seed.parent.parent
        self.state=self.root/'local-data/assistant-registry.json'
        with LOCK:
            if not self.state.exists():
                c=read(self.seed)
                if __import__('os').environ.get('APP_START_EMPTY')=='1':
                    write(self.state,{'default_id':None,'assistants':[]});return
                aid=c.get('web',{}).get('public_slug') or secrets.token_hex(6)
                if not re.fullmatch(r'[a-z0-9-]{2,60}',aid):raise PipelineError('无效助手标识。')
                p=Pipeline(self.seed,allow_empty=True)
                write(self.state,{'default_id':aid,'assistants':[{'id':aid,'config_path':str(self.seed.relative_to(self.root)),
                    'active_path':str(self.seed.relative_to(self.root)) if p.rows else None,
                    'active_index':p.index_id if p.rows else None,'enabled':bool(p.rows),
                    'created_at':datetime.now(timezone.utc).isoformat(),'job':{'state':'idle','message':'暂无构建任务'}}]})
    def data(self):return read(self.state)
    def get(self,aid=None):
        data=self.data();aid=aid or data['default_id']
        record=next((x for x in data['assistants'] if x['id']==aid and not x.get('deleted_at')),None)
        if not record:raise PipelineError('找不到这个助手。')
        return record
    def remove(self,aid):
        with LOCK:
            record=self.get(aid)
            if record.get('job',{}).get('state')=='running':raise PipelineError('索引正在构建，请完成后再删除助手。')
            data=self.data();target=next(x for x in data['assistants'] if x['id']==aid)
            target.update(deleted_at=datetime.now(timezone.utc).isoformat(),enabled=False)
            if data['default_id']==aid:data['default_id']=next((x['id'] for x in data['assistants'] if not x.get('deleted_at')),None)
            write(self.state,data)
            return {'ok':True,'message':'助手已删除，可在已删除助手中恢复。'}
    def restore(self,aid):
        with LOCK:
            data=self.data();record=next((x for x in data['assistants'] if x['id']==aid and x.get('deleted_at')),None)
            if not record:raise PipelineError('找不到已删除的助手。')
            record.pop('deleted_at');record['enabled']=False
            if not data['default_id']:data['default_id']=aid
            write(self.state,data)
            return {'ok':True,'message':'助手已恢复，当前暂停服务，请检查版本后启用。'}
    def path(self,record,active=False):
        rel=record.get('active_path') if active else record['config_path']
        if not rel:raise PipelineError('助手尚未准备好知识索引。')
        return safe_path(self.root,rel)
    def update(self,aid,**fields):
        with LOCK:
            data=self.data();record=next(x for x in data['assistants'] if x['id']==aid);record.update(fields);write(self.state,data)
            return record
    def summary(self,record):
        c=read(self.path(record));p=Pipeline(self.path(record),allow_empty=True)
        return {**record,'config_path':None,'active_path':None,'name':c['web']['assistant_name'],
            'company':c['web']['company_name'],'description':c['web']['description'],
            'brand':{k:c['web'].get(k) for k in ('accent','theme','logo_asset','logo_version')},
            'documents':len({x['document_id'] for x in (Pipeline(self.path(record,active=True)).rows if record.get('active_path') else p.rows)}),'chunks':len(Pipeline(self.path(record,active=True)).rows) if record.get('active_path') else len(p.rows),
            'ready':bool(record.get('active_path')),'pending_index':SettingsStore(self.path(record)).revision()!=record['published_draft_revision'] if record.get('published_draft_revision') else p.index_id!=record.get('active_index'),
            'url':'/a/'+record['id'],'settings_url':'/settings?assistant='+record['id']}
    def create(self,values):
        with LOCK:
            if not isinstance(values,dict):raise PipelineError('初始化信息无效。')
            name=values.get('name','').strip();company=values.get('company','').strip()
            if not 1<=len(name)<=60 or not 1<=len(company)<=100:raise PipelineError('请填写助手名称和企业名称。')
            accent=values.get('accent','#2479b9');theme=values.get('theme','clean')
            if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent) or theme not in ('clean','soft','night'):raise PipelineError('品牌主题无效。')
            aid=secrets.token_hex(6);target=self.root/'local-data/assistants'/aid
            path=clone_config(self.seed,target,tenant='assistant-'+aid);c=read(path)
            c['web'].update(assistant_name=name,company_name=company,description=values.get('description','')[:300] or '从企业知识中查找有依据的回答。',accent=accent,theme=theme,
                welcome_title=values.get('welcome_title','')[:100] or f'你好，我是{name}',welcome_subtitle=values.get('welcome_subtitle','')[:200] or '有什么可以帮你？',logo_asset='',examples=values.get('examples',[])[:8])
            if not all(isinstance(x,str) and len(x)<=300 for x in c['web']['examples']):raise PipelineError('示例问题无效。')
            profile_id=values.get('profile')
            profiles=read(self.root/'config/knowledge-profiles.json')
            profile=next((p for p in profiles if p['id']==profile_id),None)
            if profile_id and not profile:raise PipelineError('未知知识版本。')
            if profile is None:
                for child in target.iterdir():
                    if child.name!='config':
                        if child.is_dir():shutil.rmtree(child)
                        else:child.unlink()
                c['answer']['messages']=copy.deepcopy(read(self.seed)['onboarding']['generic_messages'])
                raw=b'';rp=target/'artifacts/empty/chunks.jsonl';rp.parent.mkdir(parents=True,exist_ok=True);rp.write_bytes(raw)
                import hashlib
                write(rp.parent/'chunk-report.json',{'chunks_sha256':hashlib.sha256(raw).hexdigest(),'chunk_count':0})
                c.update(chunks_path='artifacts/empty/chunks.jsonl',chunk_report_path='artifacts/empty/chunk-report.json')
                c['web']['knowledge_profile']='empty'
                write(target/'config/knowledge-profiles.json',[{'id':'empty','name':'尚未上传知识','chunks_path':c['chunks_path'],'chunk_report_path':c['chunk_report_path'],'snapshot_date':None}])
                c['retrieval']['source_groups']=[]
                c['retrieval']['query_rules']=[]
                template=safe_path(self.root,read(self.seed).get('onboarding',{})['generic_prompt_path']).read_text()
                atomic(target/'prompts/business.txt',template);c['answer']['prompt_path']='prompts/business.txt'
            else:
                c['web']['knowledge_profile']=profile_id
                for key in ('chunks_path','chunk_report_path'):
                    c[key]=profile[key]
                    prefix=Path(c[key]).parts[0]
                    if not (target/prefix).exists():shutil.copytree(safe_path(self.root,prefix),target/prefix)
            foundation=self.root/'prompts/platform-rules-v1.txt'
            if foundation.exists():atomic(target/'prompts/platform-rules-v1.txt',foundation.read_text())
            c['business']=read(self.root/'config/business-defaults.json')
            write(path,c)
            data=self.data();record={'id':aid,'config_path':str(path.relative_to(self.root)),'active_path':None,'active_index':None,'enabled':False,
                'created_at':datetime.now(timezone.utc).isoformat(),'job':{'state':'idle','message':'请准备知识并构建索引'}}
            if values.get('unified_flow'):
                record.update(configuration_step=1,configuration_complete=False,pending_setup=True)
            data['assistants'].append(record)
            if data['default_id'] is None:data['default_id']=aid
            write(self.state,data)
            return self.summary(record)
    def freeze_active(self,record):
        if not record.get('active_path'):return
        source=self.path(record,active=True)
        # Freeze only if settings are about to overwrite that same active file.
        if source!=self.path(record):return
        target=self.root/'local-data/assistants'/record['id']/'snapshots'/secrets.token_hex(8)
        frozen=clone_config(source,target)
        self.update(record['id'],active_path=str(frozen.relative_to(self.root)))
    def invalidate_candidate(self,aid,reason):
        record=self.get(aid);fields={'candidate_path':None,'candidate_index':None}
        job=record.get('job',{})
        if job.get('state')=='done':fields['job']={**job,'stale':True,'message':reason+' 上次构建结果仅供追溯，请重新构建后预览发布。'}
        self.update(aid,**fields)
    def after_save(self,aid):
        # Draft settings never replace a published snapshot.
        record=self.get(aid)
        if record.get('build_signature','')[:64]==SettingsStore(self.path(record)).revision():return
        self.invalidate_candidate(aid,'草稿配置已变化。')
    def activate(self,aid):
        record=self.get(aid)
        if not record.get('candidate_path'):raise PipelineError('请先构建并预览候选版本。')
        if record.get('previewed_revision')!=record.get('candidate_revision'):raise PipelineError('请先预览本次候选版本。')
        c=read(safe_path(self.root,record['candidate_path']))
        import os
        if not all(os.environ.get(k) for k in (c['embedding']['api_key_env'],c['answer']['api_key_env'],c['database']['url_env'],c['database']['service_key_env'])):raise PipelineError('请先配置模型和数据库连接。')
        history=record.get('publications',[])
        if not history and record.get('active_path'):history.append({'version':record.get('active_index'),'config_path':record['active_path'],'published_at':record.get('created_at'),'kind':'existing'})
        history.append({'version':record['candidate_index'],'config_path':record['candidate_path'],'published_at':datetime.now(timezone.utc).isoformat(),'revision':record['candidate_revision']})
        return self.summary(self.update(aid,job={**record.get('job',{}),'message':'版本已发布；新问题使用当前服务版本。','published':True},publications=history,published_signature=record.get('build_signature'),published_draft_revision=SettingsStore(self.path(record)).revision(),enabled=True,active_path=record['candidate_path'],active_index=record['candidate_index'],candidate_path=None,candidate_index=None))
