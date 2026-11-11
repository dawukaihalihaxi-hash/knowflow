"""Small assistant editor contract; privileged fields never round-trip through its form."""
import copy
from assistant_catalog import read
from settings_store import SettingsStore,LOCK
from document_store import DocumentStore
from rag_pipeline import PipelineError

WEB_KEYS=('assistant_name','company_name','description','welcome_title','welcome_subtitle','examples','accent','theme')
TOP_KEYS=('vector_top_k','keyword_top_k','rerank_candidate_limit','context_top_k')

def model_options(catalog,c):
    options=read(catalog.root/'config/model-options.json')
    for section in ('answer','embedding'):
        if not any(x['id']==c[section]['model'] for x in options[section]):
            options[section].append({'id':c[section]['model'],'label':c[section]['model'],**({'dimensions':c[section]['dimensions']} if section=='embedding' else {})})
    return options

def editor_view(catalog,aid):
    record=catalog.get(aid);store=SettingsStore(catalog.path(record));state=store.view();c=state['config']
    docs=DocumentStore(catalog,aid);plan=docs.plan();record=catalog.get(aid)
    dirty=store.revision()!=record['published_draft_revision'] if record.get('published_draft_revision') else not record.get('active_path') or store.revision()!=SettingsStore(catalog.path(record,active=True)).revision()
    job=record.get('job',{})
    active_config=read(catalog.path(record,active=True)) if record.get('active_path') else None
    status='building' if job.get('state')=='running' else 'ready_to_publish' if record.get('candidate_path') else 'needs_build' if dirty or plan['added'] or plan['removed'] or plan['replaced'] else 'published' if record.get('enabled') else 'disabled'
    if record.get('configuration_complete') is False and status not in ('building','ready_to_publish'):status='incomplete'
    return {'revision':state['revision'],'web':{k:c['web'].get(k) for k in WEB_KEYS},
      'logo':{k:c['web'].get(k,'') for k in ('logo_asset','logo_version')},
      'top_k':{k:c['retrieval'][k] for k in TOP_KEYS},'top_k_range':{'min':1,'max':100},
      'business':c['business'],'models':{'answer':c['answer']['model'],'embedding':c['embedding']['model'],'dimensions':c['embedding']['dimensions']},
      'current_models':{k:active_config[k]['model'] for k in ('answer','embedding')} if active_config else None,'model_options':model_options(catalog,c),'services':{'answer':state['credentials'].get(c['answer']['api_key_env'],False),
       'embedding':state['credentials'].get(c['embedding']['api_key_env'],False),'database':all(state['credentials'].get(c['database'][k],False) for k in ('url_env','service_key_env'))},
      'lifecycle':{'state':status,'enabled':record['enabled'],'configuration_complete':record.get('configuration_complete',True),
        'previewed':bool(record.get('candidate_revision')) and record.get('previewed_revision')==record.get('candidate_revision')},
      'knowledge':plan,'job':job,'history':[{k:h.get(k) for k in ('version','published_at','kind','revision')} for h in record.get('publications',[])]}

def save_editor(catalog,aid,values):
    allowed={'revision','web','top_k','business','models'}
    if not isinstance(values,dict) or set(values)-allowed:raise PipelineError('不允许提交工程参数或未知字段。')
    with LOCK:
        record=catalog.get(aid);store=SettingsStore(catalog.path(record));state=store.view();c=copy.deepcopy(state['config'])
        for field,keys in [('web',WEB_KEYS),('top_k',TOP_KEYS),('business',tuple(read(catalog.root/'config/business-defaults.json')))]:
            if not isinstance(values.get(field),dict) or set(values[field])!=set(keys):raise PipelineError('配置字段不完整或包含未允许的字段。')
        c['web'].update(values['web']);c['retrieval'].update(values['top_k']);c['business']=values['business']
        choices=model_options(catalog,c)
        if not isinstance(values.get('models'),dict) or set(values['models'])!={'answer','embedding'}:raise PipelineError('模型选择无效。')
        for section in ('answer','embedding'):
            choice=next((x for x in choices[section] if x['id']==values['models'][section]),None)
            if not choice:raise PipelineError('请选择平台已登记的模型。')
            c[section]['model']=choice['id']
            if section=='embedding':c[section]['dimensions']=choice['dimensions']
        # Validate before freezing; a failed submission must leave published metadata untouched.
        store.validate(copy.deepcopy(c),store.read(),state['prompt'])
        if values.get('revision')!=store.revision():raise PipelineError('草稿已变化，请刷新后再提交。')
        catalog.freeze_active(record)
        store.save({'revision':values['revision'],'config':c,'prompt':state['prompt']})
        catalog.after_save(aid)
        complete=bool(DocumentStore(catalog,aid).plan()['selected_documents'])
        catalog.update(aid,configuration_complete=complete,configuration_step=5 if complete else 1,pending_setup=False)
        return editor_view(catalog,aid)
