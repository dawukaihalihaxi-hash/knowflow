"""Local preview and authenticated production entrypoints for the configured RAG app."""
from collections import defaultdict,deque
from datetime import timedelta
import hashlib
import copy
import re
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit
from werkzeug.exceptions import HTTPException
from flask import Flask,request,jsonify,render_template,session,redirect,Response,send_file,g
from rag_pipeline import Pipeline,PipelineError,read_env
from answer_service import AnswerService
from settings_store import SettingsStore,LOCK,atomic
from assistant_catalog import AssistantCatalog,read,write,clone_config
from document_store import DocumentStore
from document_extractors import capabilities
from editor_service import editor_view,save_editor,model_options,WEB_KEYS,TOP_KEYS
from trace_store import save_trace,list_traces,load_trace
from trial_service import CloudWorkspace,TrialError

def create_app(config_path,testing=False):
    config_path=Path(config_path).resolve();root=config_path.parent.parent;read_env(root/'.env')
    prod=os.environ.get('APP_ENV')=='production'
    trial_mode=os.environ.get('APP_PUBLIC_TRIAL')=='1'
    public_demo=os.environ.get('APP_PUBLIC_DEMO')=='1' and not trial_mode
    if prod and not all(os.environ.get(k) for k in ('APP_ADMIN_PASSWORD','APP_USER_PASSWORD','APP_SESSION_SECRET')):
        raise RuntimeError('Production requires administrator/user passwords and session secret')
    app=Flask(__name__,template_folder=str(root/'templates'),static_folder=str(root/'static'))
    app.jinja_env.policies['json.dumps_kwargs']={'sort_keys':True,'ensure_ascii':False}
    def fingerprint():
        inputs=list(Path(__file__).parent.glob('*.py'))
        for folder in ('templates','static'):
            inputs.extend(p for p in (root/folder).rglob('*') if p.is_file())
        return hashlib.sha256(b''.join(p.read_bytes() for p in sorted(inputs))).hexdigest()[:16]
    boot_version=fingerprint()
    app.config['KNOWFLOW_VERSION']=boot_version
    @app.context_processor
    def platform_brand_context():
        return {'public_demo':readonly_page(),'trial_mode':trial_mode,'is_platform_admin':admin(),'trial_limits':cloud.limits if cloud else {},'app_version':boot_version,'platform_brand':json.loads((root/'config/platform-brand.json').read_text(encoding='utf-8'))}

    app.config.update(TESTING=testing,SECRET_KEY=os.environ.get('APP_SESSION_SECRET') or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',SESSION_COOKIE_SECURE=prod,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7) if trial_mode else timedelta(hours=8),MAX_CONTENT_LENGTH=11*1024*1024)
    cloud=CloudWorkspace(config_path) if trial_mode else None
    cloud_mutex=threading.Lock()
    catalog=AssistantCatalog(config_path)
    if cloud and cloud.revision<0:
        cloud.acquire()
        try:cloud.restore();cloud.persist()
        finally:cloud.release()
    store=SettingsStore(config_path);recent=defaultdict(deque);slots=threading.BoundedSemaphore(2)
    job={'state':'idle','message':'暂无构建任务'};joblock=threading.Lock();app.extensions['settings_store']=store
    def local():
        return not prod and request.remote_addr in ('127.0.0.1','::1') and request.host.split(':')[0] in ('localhost','127.0.0.1','[::1]') and not request.headers.get('X-Forwarded-For')
    def admin():return local() or session.get('role')=='admin'
    def owns(record):return bool(cloud and cloud.owns(record,session['client']))
    def editable(record):return admin() or owns(record) and not cloud.expired(record)
    def readonly_page():
        if not trial_mode:return public_demo
        aid=request.args.get('assistant') or (catalog.data()['default_id'] if request.path=='/settings' else None)
        return bool(aid and not editable(catalog.get(aid)))
    def visible(record):return admin() or not record.get('owner') or owns(record)
    def acquire_cloud():
        if not cloud:return
        if not cloud_mutex.acquire(timeout=3):raise TrialError('正在处理另一项操作，请稍后重试。',409)
        g.cloud_mutex=True;g.cloud_owned=False
        if not cloud.building:
            cloud.acquire();g.cloud_owned=True;cloud.restore()
        elif request.method=='POST' and not re.fullmatch(r'/api/assistants/[a-z0-9-]+/ask',request.path):
            raise TrialError('正在构建知识，请完成后再修改。',409)
    @app.teardown_request
    def cleanup_cloud(error):
        if getattr(g,'cloud_mutex',False):
            try:
                if getattr(g,'cloud_owned',False) and not cloud.building:cloud.release()
            finally:g.cloud_mutex=False;cloud_mutex.release()
    @app.errorhandler(TrialError)
    def trial_error(error):return jsonify(error=str(error)),error.status

    @app.before_request
    def guard():
        if request.host.split(':')[0] not in ('localhost','127.0.0.1','[::1]') and not prod:return jsonify(error='本地预览仅允许本机访问。'),403
        session.setdefault('csrf',secrets.token_hex(24));session.setdefault('client',secrets.token_hex(16))
        supplied=request.headers.get('X-KnowFlow-Version')
        if supplied and supplied!=boot_version:return jsonify(error='页面与服务版本不一致，请刷新。',code='version_mismatch'),409
        if request.path.startswith('/api/') and fingerprint()!=boot_version:return jsonify(error='服务加载的代码已过期，请重启本机服务并刷新。',code='restart_required'),409
        if request.path in ('/healthz','/login') or request.path.startswith('/static/'):return
        if (public_demo or trial_mode) and not session.get('role'):session['role']='user'
        if trial_mode:
            session.permanent=True
            acquire_cloud()
            aid=(request.view_args or {}).get('aid') or request.args.get('assistant') or (catalog.data()['default_id'] if request.path in ('/settings','/api/build') else None)
            management=request.path in ('/settings','/api/build') or request.path.startswith(('/api/admin/assistants/','/admin/assistants/'))
            if aid:
                record=catalog.get(aid) if not request.path.endswith('/restore') else next((r for r in catalog.data()['assistants'] if r['id']==aid),None)
                if not record:raise TrialError('找不到这个助手。',404)
                if management:
                    if not visible(record):raise TrialError('没有这个助手的管理权限。')
                    if (request.method=='POST' or 'preview' in request.path or request.path.endswith('/configuration')) and not editable(record):raise TrialError('此助手为只读样例，或你的试用期已结束。')
                elif cloud.expired(record) and not admin():raise TrialError('这个助手的试用期已结束。',410)
                elif record.get('owner') and not record.get('enabled') and not editable(record):raise TrialError('助手尚未发布。',404)
            if request.path.startswith('/a/') and '/sources/' in request.path and not admin() and not owns(record):raise TrialError('原始资料仅创建者可以查看。')
            allowed=request.path in ('/admin','/setup','/settings','/api/admin/editor-defaults','/api/admin/assistants','/api/admin/deleted-assistants','/api/admin/document-capabilities','/api/build') or bool(re.fullmatch(r'/(?:api/admin|admin)/assistants/[a-z0-9-]+/(?:editor|configuration|delete|restore|resume|pause|activate|logo|preview|preview-chat|preview-theme|preview-brand|preview-ask|documents(?:/[^/]+(?:/(?:action|original|update))?)?)',request.path))
            if (request.path.startswith(('/api/admin','/admin/','/api/settings','/api/credentials','/api/connection')) or request.path=='/api/build') and not allowed and not admin():raise TrialError('底层设置仅允许管理员访问。')
            g.trial_allowed=allowed
        if public_demo and request.method=='POST' and request.path not in ('/login','/logout','/api/ask') and not re.fullmatch(r'/api/assistants/[a-z0-9-]+/ask',request.path):return jsonify(error='作品演示站使用固定知识快照，管理操作不开放。'),403
        if not local() and session.get('role') not in ('user','admin'):
            return (jsonify(error='请先登录。'),401) if request.path.startswith('/api/') else redirect('/login')
        if request.path in ('/admin','/setup','/settings') or request.path.startswith(('/admin/','/api/admin','/api/settings','/api/credentials','/api/build','/api/connection')):
            demo_read=public_demo and request.method=='GET' and (request.path in ('/admin','/settings','/api/admin/assistants','/api/admin/deleted-assistants','/api/admin/document-capabilities','/api/build') or re.fullmatch(r'/api/admin/assistants/[a-z0-9-]+/(editor|documents(?:/[^/]+(?:/original)?)?)',request.path))
            if not admin() and not demo_read and not (trial_mode and getattr(g,'trial_allowed',False)):return jsonify(error='需要管理员权限。'),403
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):return jsonify(error='页面验证失效，请刷新。',code='csrf_expired'),403
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=request.host:return jsonify(error='拒绝跨站请求。'),403
    @app.after_request
    def headers(response):
        if cloud and getattr(g,'cloud_owned',False) and not cloud.building:
            try:
                if response.status_code<400:cloud.persist()
                else:cloud.restore(force=True)
            except Exception:
                cloud.needs_restore=True
                app.logger.exception('trial_checkpoint_failed')
                response=jsonify(error='云端保存未确认成功，请刷新后重试。');response.status_code=503
            finally:
                try:cloud.release()
                except Exception:app.logger.exception('trial_lease_release_failed')
                g.cloud_owned=False
        response.headers['Cache-Control']='no-store'
        response.headers['X-KnowFlow-Version']=boot_version
        response.headers['X-Content-Type-Options']='nosniff';response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response
    @app.errorhandler(HTTPException)
    def http_error(e):
        if request.path.startswith('/api/'):
            app.logger.warning('api_http_error status=%s method=%s path=%s',e.code,request.method,request.path)
            return jsonify(error={404:'接口不存在，请核对服务版本及请求地址。',405:'请求方法不支持。',401:'请先登录。',403:'没有操作权限。'}.get(e.code,'请求未完成。'),code='http_error'),e.code
        return e
    @app.errorhandler(Exception)
    def unexpected_error(e):
        incident=secrets.token_hex(6)
        app.logger.exception('request_failed incident=%s method=%s path=%s',incident,request.method,request.path)
        return jsonify(error='服务处理失败，请重试或按事件编号检查日志。',incident=incident),500
    @app.errorhandler(PipelineError)
    def pipeline_error(e):return jsonify(error=str(e)),400
    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    @app.errorhandler(ValueError)
    def invalid_data(e):return jsonify(error='配置或请求字段无效，请检查输入。'),400
    @app.errorhandler(400)
    def malformed(e):return jsonify(error='请求格式无效。'),400
    @app.errorhandler(413)
    def too_large(e):return jsonify(error='请求内容过大。'),413
    @app.route('/api/session')
    def session_token():return jsonify(csrf=session['csrf'])
    @app.route('/healthz')
    def health():return jsonify(status='ok',version=boot_version,api_version=2,source_current=fingerprint()==boot_version)
    @app.route('/api/admin/runtime')
    def runtime_info():return jsonify(version=boot_version,api_version=2,source_current=fingerprint()==boot_version,root=str(root),pid=os.getpid())
    @app.route('/login',methods=['GET','POST'])
    def login():
        if request.method=='GET':return render_template('login.html',csrf=session['csrf'])
        values=request.get_json(silent=True) or {};pwd=values.get('password','')
        key=request.remote_addr or 'unknown';now=time.monotonic();q=recent['login:'+key]
        while q and now-q[0]>60:q.popleft()
        if len(q)>=6:return jsonify(error='请稍后再尝试登录。'),429
        q.append(now)
        if not hmac.compare_digest(values.get('csrf',''),session['csrf']):return jsonify(error='请刷新登录页面。'),403
        role=None
        for name,candidate in [('APP_ADMIN_PASSWORD','admin'),('APP_USER_PASSWORD','user')]:
            expected=os.environ.get(name)
            if expected and isinstance(pwd,str) and hmac.compare_digest(pwd,expected):role=candidate;break
        if role is None:return jsonify(error='口令不正确。'),401
        client=session.get('client',secrets.token_hex(16));session.clear();session.update(role=role,csrf=secrets.token_hex(24),client=client);session.permanent=True
        return jsonify(ok=True)
    @app.route('/logout',methods=['POST'])
    def logout():
        client=session.get('client',secrets.token_hex(16));session.clear();session.update(client=client,csrf=secrets.token_hex(24));return jsonify(ok=True)
    @app.route('/')
    def home():
        if not any(not r.get('deleted_at') for r in catalog.data()['assistants']):
            return redirect('/setup') if admin() else ('工作空间尚未初始化，请联系管理员。',503)
        return redirect('/admin' if admin() or public_demo or trial_mode else '/a/'+catalog.get()['id'])
    @app.route('/settings')
    def settings():
        record=catalog.get(request.args.get('assistant'));return render_template('settings.html',csrf=session['csrf'],authenticated=not local(),assistant_id=record['id'],creating=False,onboarding=record.get('configuration_complete') is False)
    @app.route('/api/status')
    def status():
        with LOCK:
            c=store.read();p=Pipeline(config_path);count=len(p.rows)
        return jsonify(web=c['web'],documents=len({x['document_id'] for x in p.rows}),chunks=count,
                       model=c['answer']['model'],rerank=False,admin=admin(),
                       snapshot=next((x.get('snapshot_date','') for x in json.loads(store.registry.read_text()) if x['id']==c['web']['knowledge_profile']),''))
    @app.route('/api/ask',methods=['POST'])
    def ask():return assistant_ask(catalog.get()['id'])
    def selected_store():return SettingsStore(catalog.path(catalog.get(request.args.get('assistant'))))
    @app.route('/api/settings',methods=['GET','POST'])
    def settings_api():
        current=selected_store();record=catalog.get(request.args.get('assistant'))
        def view():
            result=current.view();r=catalog.get(record['id'])
            result['advanced_allowed']=os.environ.get('APP_ALLOW_ADVANCED_SETTINGS')=='1'
            result['pending_index']=result['index_id']!=r.get('active_index')
            result['effective_index']=r.get('active_index')
            result['candidate_index']=r.get('candidate_index')
            result['pending_configuration']=not r.get('active_path') or current.revision()!=SettingsStore(catalog.path(r,active=True)).revision()
            return result
        if request.method=='GET':return jsonify(view())
        with LOCK:
            catalog.freeze_active(record)
            values=request.get_json()
            if os.environ.get('APP_ALLOW_ADVANCED_SETTINGS')!='1':
                old=current.read();incoming=values['config']
                for key in incoming:
                    if key=='retrieval':
                        protected=copy.deepcopy(incoming[key])
                        for k in ('vector_top_k','keyword_top_k','rerank_candidate_limit','context_top_k'):protected[k]=old[key][k]
                        if protected!=old[key]:raise PipelineError('工程检索参数仅允许平台运维修改。')
                    elif key not in ('web','business') and incoming[key]!=old.get(key):raise PipelineError('底层参数仅允许平台运维修改。')
                if values.get('prompt')!=current.view()['prompt']:raise PipelineError('原始Prompt由平台管理。')
            current.save(values);catalog.after_save(record['id']);return jsonify(view())
    @app.route('/api/credentials',methods=['POST'])
    def credentials():
        if os.environ.get('APP_ALLOW_ADVANCED_SETTINGS')!='1':return jsonify(error='服务凭据仅允许平台运维修改。'),403
        return jsonify(credentials=store.save_credentials(request.get_json()))
    @app.route('/api/connection',methods=['POST'])
    def connection():
        if os.environ.get('APP_ALLOW_ADVANCED_SETTINGS')!='1':return jsonify(error='连接诊断仅允许平台运维执行。'),403
        target=(request.get_json() or {}).get('target')
        with LOCK:p=Pipeline(selected_store().path,allow_empty=True)
        if target=='embedding':
            dims=len(p.embed_texts(['连接测试'])[0]);return jsonify(message=f'模型连接成功 · {dims}维')
        if target=='database':
            rows=p.db('knowledge_indexes?select=state&limit=1');return jsonify(message='数据库连接成功')
        raise PipelineError('未知连接类型。')
    @app.route('/admin')
    def admin_page():return render_template('admin.html',csrf=session['csrf'],admin=True,authenticated=not local())
    @app.route('/setup')
    def setup_page():return render_template('settings.html',csrf=session['csrf'],admin=True,authenticated=not local(),assistant_id='',creating=True,onboarding=True)
    @app.route('/api/admin/editor-defaults')
    def editor_defaults():
        state=store.view();c=state['config']
        web={k:c['web'].get(k) for k in WEB_KEYS}
        web.update(assistant_name='',company_name='',description='',welcome_title='你好，有什么可以帮你？',welcome_subtitle='',examples=[])
        return jsonify(revision=None,web=web,logo={},top_k={k:c['retrieval'][k] for k in TOP_KEYS},
          business=read(root/'config/business-defaults.json'),models={'answer':c['answer']['model'],'embedding':c['embedding']['model'],'dimensions':c['embedding']['dimensions']},
          model_options=model_options(catalog,c),services={'answer':state['credentials'].get(c['answer']['api_key_env'],False),'embedding':state['credentials'].get(c['embedding']['api_key_env'],False),
          'database':all(state['credentials'].get(c['database'][k],False) for k in ('url_env','service_key_env'))},
          lifecycle={'state':'draft','enabled':False,'configuration_complete':False,'previewed':False},knowledge={},history=[],job={})
    @app.route('/api/admin/assistants/<aid>/editor',methods=['GET','POST'])
    def assistant_editor(aid):
        return jsonify(editor_view(catalog,aid) if request.method=='GET' else save_editor(catalog,aid,request.get_json()))
    @app.route('/api/admin/assistant-defaults')
    def assistant_defaults():
        result=store.view();result['advanced_allowed']=os.environ.get('APP_ALLOW_ADVANCED_SETTINGS')=='1';c=result['config']
        c['web'].update(assistant_name='',company_name='',description='',welcome_title='你好，有什么可以帮你？',welcome_subtitle='',examples=[],logo_asset='')
        c['retrieval']['source_groups']=[];c['retrieval']['query_rules']=[]
        c['answer']['messages']=dict(c['onboarding']['generic_messages'])
        result['prompt']=(root/c['onboarding']['generic_prompt_path']).read_text()
        return jsonify(result)
    @app.route('/api/admin/assistants/<aid>/configuration',methods=['GET','POST'])
    def configuration_progress(aid):
        with LOCK:
            record=catalog.get(aid)
            if request.method=='GET':return jsonify(step=record.get('configuration_step',0),complete=record.get('configuration_complete',True))
            step=(request.get_json() or {}).get('step')
            if type(step) is not int or not 1<=step<=5:raise PipelineError('无效配置步骤。')
            previous=record.get('configuration_step',0)
            # Navigation is free; validate prerequisites only when saving configuration.
            if step>=2:
                pipeline=Pipeline(catalog.path(record),allow_empty=True)
                approved=any(d['status'] in ('approved','indexed','needs_review') for d in DocumentStore(catalog,aid).all())
                if not pipeline.rows and not approved:raise PipelineError('请先上传并预览确认至少一份知识资料。')
            if step>=3:
                credentials=SettingsStore(catalog.path(record)).view()['credentials']
                if not all(credentials.get(k) for k in ('DASHSCOPE_API_KEY','SUPABASE_URL','SUPABASE_SECRET_KEY')):raise PipelineError('请先配置模型和数据库服务端凭据。')
            if step==5 and not SettingsStore(catalog.path(record)).view()['prompt'].strip():raise PipelineError('请填写业务Prompt。')
            catalog.update(aid,configuration_step=max(step,previous),configuration_complete=step==5 or record.get('configuration_complete',False),pending_setup=step!=5)
            return jsonify(step=max(step,previous),complete=step==5)
    @app.route('/api/admin/assistants',methods=['GET','POST'])
    def assistants():
        if request.method=='GET':
            rows=[]
            for r in catalog.data()['assistants']:
                if r.get('deleted_at') or not visible(r):continue
                if r.get('pending_setup') and not trial_mode:continue
                row=catalog.summary(r);row.update(can_manage=editable(r),expires_at=r.get('expires_at'),sample=not bool(r.get('owner')))
                # Never expose ownership secrets or server file paths.
                if trial_mode:row={k:row[k] for k in ('id','name','description','brand','created_at','documents','chunks','enabled','ready','configuration_complete','pending_index','url','settings_url','can_manage','expires_at','sample') if k in row}
                rows.append(row)
            return jsonify(assistants=rows,profiles=[] if trial_mode else json.loads(store.registry.read_text()),trial=cloud.limits if cloud else None)
        values=request.get_json()
        if cloud and not admin():
            cloud.check_create(catalog,session['client'])
            if not isinstance(values,dict) or values.get('profile'):raise TrialError('试用助手请上传自己的资料。')
            values={**values,'unified_flow':True}
        result=catalog.create(values)
        if cloud and not admin():cloud.attach(catalog,result['id'],session['client'])
        return jsonify(result),201
    @app.route('/api/admin/assistants/<aid>/delete',methods=['POST'])
    def delete_assistant(aid):return jsonify(catalog.remove(aid))
    @app.route('/api/admin/assistants/<aid>/restore',methods=['POST'])
    def restore_assistant(aid):return jsonify(catalog.restore(aid))
    @app.route('/api/admin/deleted-assistants')
    def deleted_assistants():
        return jsonify(assistants=[{'id':r['id'],'name':read(catalog.path(r))['web']['assistant_name'],'deleted_at':r['deleted_at']} for r in catalog.data()['assistants'] if r.get('deleted_at') and visible(r)])
    @app.route('/admin/assistants/<aid>/traces')
    def traces_page(aid):
        catalog.get(aid);tid=request.args.get('trace')
        selected=load_trace(root,aid,tid) if tid else None
        return render_template('traces.html',csrf=session['csrf'],authenticated=not local(),assistant_id=aid,records=list_traces(root,aid),trace=selected)
    @app.route('/api/admin/assistants/<aid>/traces/<tid>')
    def trace_detail(aid,tid):
        catalog.get(aid);return jsonify(load_trace(root,aid,tid))
    @app.route('/api/admin/assistants/<aid>/resume',methods=['POST'])
    def resume_assistant(aid):
        with LOCK:
            record=catalog.get(aid)
            if not record.get('active_path') or record.get('configuration_complete') is False:raise PipelineError('没有可以恢复服务的已发布版本。')
            Pipeline(catalog.path(record,active=True))
            return jsonify(catalog.summary(catalog.update(aid,enabled=True)))
    @app.route('/api/admin/assistants/<aid>/activate',methods=['POST'])
    def activate(aid):
        if catalog.get(aid).get('configuration_complete') is False:raise PipelineError('请先完成五步助手配置。')
        with LOCK:
            record=catalog.get(aid)
            signature=SettingsStore(catalog.path(record)).revision()+DocumentStore(catalog,aid).revision()
            if signature!=record.get('build_signature'):raise PipelineError('配置或资料已变化，请重新构建。')
            result=catalog.activate(aid)
            return jsonify(result)
    @app.route('/api/admin/document-capabilities')
    def document_capabilities():
        result=capabilities()
        if cloud:result.update(max_bytes=cloud.limits['file_bytes'],max_batch_files=cloud.limits['files'])
        return jsonify(result)
    @app.route('/api/admin/assistants/<aid>/documents',methods=['GET','POST'])
    def documents(aid):
        docs=DocumentStore(catalog,aid)
        if request.method=='GET':return jsonify(documents=docs.listing())
        file=request.files.get('file')
        if not file:raise PipelineError('请选择文档。')
        content=file.stream.read(10*1024*1024+1)
        if cloud:cloud.upload_check(catalog,aid,content)
        result=docs.upload(file.filename,content)
        if cloud and not result.get('duplicate'):catalog.update(aid,uploaded_bytes=catalog.get(aid).get('uploaded_bytes',0)+len(content))
        return jsonify(result),201
    @app.route('/api/admin/assistants/<aid>/documents/<did>/update',methods=['POST'])
    def update_document(aid,did):
        file=request.files.get('file')
        if not file:raise PipelineError('请选择新文件。')
        content=file.stream.read(10*1024*1024+1)
        if cloud:cloud.upload_check(catalog,aid,content,replacement=did)
        result=DocumentStore(catalog,aid).update_file(did,file.filename,content)
        if cloud and not result.get('duplicate'):catalog.update(aid,uploaded_bytes=catalog.get(aid).get('uploaded_bytes',0)+len(content))
        return jsonify(result)
    @app.route('/api/admin/assistants/<aid>/documents/<did>',methods=['GET','POST'])
    def document(aid,did):
        docs=DocumentStore(catalog,aid)
        return jsonify(docs.preview(did) if request.method=='GET' else docs.approve(did,request.get_json()))
    @app.route('/api/admin/assistants/<aid>/logo',methods=['POST'])
    def logo(aid):
        record=catalog.get(aid);file=request.files.get('file')
        if not file:raise PipelineError('请选择Logo。')
        data=file.stream.read(5*1024*1024+1)
        if len(data)>5*1024*1024:raise PipelineError('Logo不能超过5MB，请压缩图片后重试。')
        mime='image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else None
        if not mime:raise PipelineError('Logo只支持PNG、JPEG或WebP，不支持SVG。')
        fingerprint=hashlib.sha256(data).hexdigest()
        path=root/'local-data/assistants'/aid/('logo-'+fingerprint)
        with LOCK:
            current=SettingsStore(catalog.path(record))
            if request.form.get('revision') and request.form['revision']!=current.revision():raise PipelineError('配置已变化，请先刷新后再上传Logo。')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            write(path.with_suffix('.json'),{'mime':mime})
            catalog.freeze_active(record)
            for config in {catalog.path(record)}:
                c=read(config);c['web']['logo_asset']='/a/'+aid+'/logo/'+fingerprint;c['web']['logo_version']=hashlib.sha256(data).hexdigest();write(config,c)
            catalog.after_save(aid)
        return jsonify(url='/a/'+aid+'/logo/'+fingerprint,revision=current.revision(),logo_version=hashlib.sha256(data).hexdigest())
    @app.route('/a/<aid>/logo',defaults={'version':None})
    @app.route('/a/<aid>/logo/<version>')
    def serve_logo(aid,version):
        record=catalog.get(aid)
        if record.get('configuration_complete') is False and not admin() and not owns(record):return jsonify(error='请先完成助手的完整配置。'),403
        if not record['enabled'] and not admin() and not owns(record):return jsonify(error='助手尚未启用。'),404
        if version and (len(version)!=64 or any(c not in '0123456789abcdef' for c in version)):return '',404
        path=root/'local-data/assistants'/aid/('logo-'+version if version else 'logo')
        if not path.exists():return '',404
        return send_file(path,mimetype=read(path.with_suffix('.json'))['mime'])
    @app.route('/a/<aid>')
    def chat_page(aid):
        record=catalog.get(aid)
        if record.get('configuration_complete') is False:return jsonify(error='请先完成助手的完整配置。'),403
        if not record.get('active_path'):return jsonify(error='尚未发布完成构建的版本，不能预览问答。'),403
        if not record['enabled'] and not admin() and not owns(record):return jsonify(error='助手尚未启用。'),404
        return render_template('assistant_chat.html',csrf=session['csrf'],assistant_id=aid,admin=admin(),authenticated=not local())
    @app.route('/a/<aid>/theme.css')
    def theme(aid,preview=False):
        record=catalog.get(aid)
        if preview and not record.get('candidate_path'):raise PipelineError('候选版本尚未就绪。')
        c=read(root/record['candidate_path'] if preview else catalog.path(record,active=bool(record.get('active_path'))));color=c['web'].get('accent','#2479b9')
        import re
        if not re.fullmatch(r'#[0-9a-fA-F]{6}',color):color='#2479b9'
        rgb=[int(color[i:i+2],16) for i in (1,3,5)];contrast='#102131' if sum(a*b for a,b in zip(rgb,(.2126,.7152,.0722)))>170 else '#ffffff'
        return Response(':root{--accent:'+color+';--accent-text:'+contrast+'}',mimetype='text/css')
    @app.route('/api/assistants/<aid>')
    def assistant_public(aid):
        record=catalog.get(aid)
        if record.get('configuration_complete') is False:return jsonify(error='请先完成助手的完整配置。'),403
        if not record['enabled'] and not admin() and not owns(record):return jsonify(error='助手尚未启用。'),404
        c=read(catalog.path(record,active=bool(record.get('active_path'))));logo=root/'local-data/assistants'/aid/'logo'
        version=hashlib.sha256((json.dumps(c['web'],sort_keys=True)+ (hashlib.sha256(logo.read_bytes()).hexdigest() if logo.exists() else '')).encode()).hexdigest()
        return jsonify(brand=c['web'],brand_revision=version,enabled=record['enabled'],ready=bool(record.get('active_path')),admin=admin())
    @app.route('/api/assistants/<aid>/ask',methods=['POST'])
    def assistant_ask(aid,preview=False):
        record=catalog.get(aid)
        if preview:
            if not record.get('candidate_path'):raise PipelineError('请先完成候选版本构建。')
            record={**record,'active_path':record['candidate_path'],'active_index':record['candidate_index'],'enabled':True}
        if record.get('configuration_complete') is False:raise PipelineError('请先完成助手的完整配置。')
        if not record['enabled']:raise PipelineError('助手还未启用，请先完成知识索引与初始化。')
        body=request.get_json()
        if not isinstance(body,dict) or set(body)!={'question'}:raise PipelineError('只接受问题，不能指定其他助手资料。')
        with LOCK:
            p=Pipeline(catalog.path(record,active=True))
            p.blocked_documents={d.get('document_id','upload-'+d['id']) for d in DocumentStore(catalog,aid).all() if d.get('disabled')}
            if cloud:
                if not isinstance(body['question'],str) or not body['question'].strip() or len(body['question'])>cloud.limits['question_characters']:raise TrialError('问题不能为空，最多1000字。',400)
                p.cfg['answer']['parameters'].update(cloud.limits.get('answer_parameters',{}))
                p.cfg['answer']['parameters'].pop('thinking_budget',None)
                p.cfg['answer']['parameters']['max_tokens']=min(p.cfg['answer']['parameters'].get('max_tokens',cloud.limits['answer_tokens']),cloud.limits['answer_tokens'])
            options=read(root/'config/model-options.json')
            protocol=next((m.get('evidence_protocol') for m in options.get('answer',[]) if m['id']==p.cfg['answer']['model']),None)
            service=AnswerService(p,protocol=protocol)
        now=time.monotonic();q=recent[aid+':'+session['client']];limit=read(catalog.path(record,active=True))['web']['request_limit_per_minute']
        while q and now-q[0]>60:q.popleft()
        if not cloud and len(q)>=limit:return jsonify(error='提问过于频繁，请稍后再试。'),429
        if not slots.acquire(blocking=False):return jsonify(error='助手忙碌，请稍后再试。'),429
        if cloud:
            try:cloud.consume(session['client'],'question')
            except Exception:slots.release();raise
        q.append(now)
        def record_trace():
            failed=service.last_trace.get('result',{}).get('status')=='invalid_response'
            if not service.last_trace or (not failed and not p.cfg['retrieval'].get('trace_enabled',False)):return None
            try:return save_trace(root,aid,service.last_trace,p.cfg['retrieval'].get('trace_retention_count',20))
            except OSError:
                app.logger.warning('trace_storage_failed');return None
        try:
            try:result=service.answer(body['question'])
            except PipelineError:
                record_trace();raise
            current_blocked={d.get('document_id','upload-'+d['id']) for d in DocumentStore(catalog,aid).all() if d.get('disabled')}
            if current_blocked!=p.blocked_documents:raise PipelineError('资料检索权限已变化，请重新提问。')
            tid=record_trace()
            if tid:result['trace_id']=tid
            return jsonify(result)
        finally:slots.release()
    @app.route('/a/<aid>/sources/<did>')
    def source_preview(aid,did):
        record=catalog.get(aid)
        preview=admin() and request.args.get('preview')=='1' and bool(record.get('candidate_path'))
        if not preview and not record['enabled']:return '',404
        active=Pipeline(root/record['candidate_path'] if preview else catalog.path(record,active=True))
        if not any(c['document_id']=='upload-'+did for c in active.rows):return '',404
        doc=DocumentStore(catalog,aid).preview(did)
        if doc.get('disabled'):return '',404
        return Response(doc['text'],mimetype='text/plain')
    @app.route('/api/admin/assistants/<aid>/documents/<did>/original')
    def original_document(aid,did):
        docs=DocumentStore(catalog,aid);doc=docs.get(did)
        if doc.get('origin') in ('official','quarantined'):
            return send_file(docs.root/(did+'.txt'),as_attachment=True,download_name=doc['title']+'.txt',mimetype='text/plain')
        path=docs.root/(did+Path(doc['name']).suffix.lower())
        return send_file(path,as_attachment=True,download_name=doc['name'])
    @app.route('/api/admin/assistants/<aid>/documents/<did>/action',methods=['POST'])
    def document_action(aid,did):
        values=request.get_json() or {}
        return jsonify(DocumentStore(catalog,aid).change(did,values.get('action'),values.get('replacement')))
    @app.route('/admin/assistants/<aid>/preview-chat')
    def preview_chat(aid):
        record=catalog.get(aid)
        if record.get('configuration_complete') is False or not record.get('candidate_path'):raise PipelineError('配置或构建未完成，不能预览问答。')
        return render_template('assistant_chat.html',csrf=session['csrf'],assistant_id=aid,admin=True,authenticated=not local())
    @app.route('/api/admin/assistants/<aid>/preview-theme')
    def preview_theme(aid):return theme(aid,preview=True)
    @app.route('/api/admin/assistants/<aid>/preview-brand')
    def preview_brand(aid):
        record=catalog.get(aid)
        if record.get('configuration_complete') is False or not record.get('candidate_path'):raise PipelineError('候选版本尚未就绪。')
        c=read(root/record['candidate_path'])
        return jsonify(brand=c['web'],brand_revision=record['candidate_revision'],enabled=True,ready=True,admin=True)
    @app.route('/api/admin/assistants/<aid>/preview-ask',methods=['POST'])
    def preview_ask(aid):return assistant_ask(aid,preview=True)
    @app.route('/api/admin/assistants/<aid>/preview',methods=['GET'])
    def candidate_preview(aid):
        with LOCK:
            record=catalog.get(aid)
            if not record.get('candidate_path'):raise PipelineError('没有已完成的候选版本。')
            path=root/record['candidate_path'];p=Pipeline(path)
            catalog.update(aid,previewed_revision=record['candidate_revision'])
            return jsonify(index_id=p.index_id,config=read(path)['web'],chunks=len(p.rows),
                documents=list({x['document_id']:x['title'] for x in p.rows}.items()),
                samples=[{'title':x['title'],'text':x['body']} for x in p.rows[:10]],notice='离线资料预览，未调用回答模型。')
    @app.route('/api/build',methods=['GET','POST'])
    def build():
        aid=catalog.get(request.args.get('assistant'))['id'];record=catalog.get(aid)
        if request.method=='GET':
            result=dict(record['job'])
            if result.get('state')=='running' and not joblock.locked():
                result.update(state='failed',message='服务重启导致任务中断，请重试。',retryable=True)
                catalog.update(aid,job=result)
            return jsonify(result)
        if record.get('configuration_complete') is False:raise PipelineError('请先保存完整配置，再构建。')
        if not joblock.acquire(blocking=False):return jsonify(error='已有构建任务在运行。'),409
        state={'state':'running','stage':'parsing','completed':0,'total':None,'message':'开始核验已审核资料；旧版继续服务。',
          'stages':{},'metrics':{'chunks':None,'reused_vectors':None,'new_vectors':0,'written_rows':0,'confirmed_rows':0,'failed_rows':0,'index_ready':False}}
        try:
            with LOCK:
                docs=DocumentStore(catalog,aid);documents_revision=docs.revision()
                signature=SettingsStore(catalog.path(record)).revision()+documents_revision
                revision=SettingsStore(catalog.path(record)).revision()
                catalog.update(aid,candidate_path=None,candidate_index=None,job=copy.deepcopy(state))
        except Exception:
            joblock.release();raise
        labels={'parsing':'解析与文本校验','chunking':'切分','embedding':'向量化','upload':'数据库写入','verification':'完整性检查'}
        def persist():
            with LOCK:catalog.update(aid,job=copy.deepcopy(state))
            if cloud:cloud.persist()
        def progress(stage,completed,total):
            previous=state.get('stage')
            if previous!=stage and previous in state['stages']:state['stages'][previous]['state']='done'
            state.update(stage=stage,completed=completed,total=total,message=labels.get(stage,stage)+(f' {completed}/{total}' if total is not None else '：等待服务确认'))
            state['stages'][stage]={'state':'running','completed':completed,'total':total}
            if stage=='embedding' and state['metrics']['reused_vectors'] is not None:state['metrics']['new_vectors']=max(0,completed-state['metrics']['reused_vectors'])
            if stage=='upload':state['metrics']['confirmed_rows']=completed
            persist()
        def run():
            heartbeat_stop=threading.Event()
            if cloud:threading.Thread(target=cloud.heartbeat,args=(heartbeat_stop,),daemon=True).start()
            try:
                if record.get('build_signature')==signature and record.get('build_path'):
                    path=root/record['build_path']
                    selected=[d for d in docs.all() if d['status'] in ('approved','indexed','needs_review') and not d.get('removed')]
                    for i,d in enumerate(selected):docs.preview(d['id']);progress('parsing',i+1,len(selected))
                    snapshot=Pipeline(path);progress('chunking',len(snapshot.context),len(snapshot.context))
                else:path,_=docs.prepare_version(progress=progress)
                p=Pipeline(path)
                if cloud:cloud.build_check(p)
                catalog.update(aid,build_path=str(path.relative_to(root)),build_signature=signature)
                ready=cloud.ready_index(p) if cloud else False
                cache=cloud.reuse_vectors(p,catalog,record) if cloud and not ready else p.cache()
                reused=len(p.rows) if ready else len(cache['vectors'])
                state['metrics'].update(chunks=len(p.rows),total_chunks=len(p.context),reused_vectors=reused)
                progress('embedding',reused,len(p.rows))
                if cloud:cloud.consume(build_client,'vectors',len(p.rows)-reused)
                embedded={'new_vectors':0} if ready else p.embed(progress=progress)
                state['metrics']['new_vectors']=embedded.get('new_vectors',state['metrics']['new_vectors'])
                progress('upload',0,len(p.rows))
                if ready:
                    uploaded={'state':'ready','written_rows':0,'confirmed_rows':len(p.rows)}
                    progress('upload',len(p.rows),len(p.rows))
                else:uploaded=p.upload(progress=progress)
                progress('verification',0,None)
                if not isinstance(uploaded,dict) or uploaded.get('state')!='ready':raise PipelineError('云端索引完整性检查未确认就绪。')
                verified=Pipeline(path)
                if verified.index_id!=p.index_id:raise PipelineError('构建产物指纹校验不一致。')
                with LOCK:
                    if SettingsStore(catalog.path(catalog.get(aid))).revision()!=revision or docs.revision()!=documents_revision:raise PipelineError('构建期间配置或资料发生变化，请重新构建。')
                    state['stages']['verification']={'state':'done','completed':1,'total':1}
                    for phase in state['stages'].values():phase['state']='done'
                    state['metrics'].update(written_rows=uploaded.get('written_rows'),confirmed_rows=uploaded.get('confirmed_rows'),index_ready=True)
                    state.update(state='done',stage='ready',completed=1,total=1,message='候选版本完整性检查通过，预览并确认发布后才切换。',retryable=False)
                    catalog.update(aid,candidate_path=str(path.relative_to(root)),candidate_index=p.index_id,candidate_revision=secrets.token_hex(16),previewed_revision=None,job=copy.deepcopy(state))
            except Exception as e:
                app.logger.exception('build_failed assistant=%s stage=%s',aid,state.get('stage'))
                reason=str(e) if isinstance(e,PipelineError) else '构建处理失败，请检查服务日志。'
                state.update(state='failed',message=reason,retryable=True)
                state['metrics']['failed_rows']=None
                if state.get('stage') in state['stages']:state['stages'][state['stage']]['state']='failed'
                persist()
            finally:
                heartbeat_stop.set()
                try:
                    if cloud:
                        with cloud_mutex:
                            try:cloud.persist()
                            finally:cloud.release();cloud.building=False
                finally:joblock.release()
        build_client=session['client']
        if cloud:
            try:
                cloud.consume(build_client,'build');cloud.persist();cloud.building=True
            except Exception:joblock.release();raise
        threading.Thread(target=run,daemon=True).start();return jsonify(catalog.get(aid)['job']),202
    return app
