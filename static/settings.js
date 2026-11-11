'use strict';
const readOnlyDemo=document.body.dataset.demoReadonly==='true';
const $=s=>document.querySelector(s),steps=['identity','knowledge','models','retrieval','answer'];
let assistantId=$('#assistant-id').value,state,dirty=false,busy=false,documents=[],reviewing,replacing,poll;
const bindings=[];
const phases={parsing:'解析与文本校验',chunking:'切分',embedding:'向量化',upload:'数据库写入',verification:'完整性检查'};
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const buildLabel=d=>({built:'已构建',pending:'待构建',excluded:'不允许构建'})[d.build_state]||'构建状态未知';
const chunkValue=value=>value===undefined||value===null?'—':String(value);
const known=value=>value===undefined||value===null||value===''?'未知 / 未登记':String(value);
function fail(e){$('#settings-error').hidden=false;$('#settings-error').textContent=e.message;}
function notice(text){$('#save-status').textContent=text;}
function endpoint(suffix){return '/api/admin/assistants/'+encodeURIComponent(assistantId)+suffix;}
function api(url,body){return appRequest(url,{...(body!==undefined?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});}
function button(text,fn,cls='secondary-button'){const e=node('button',text,cls);e.type='button';e.onclick=async()=>{e.disabled=true;try{await fn();}catch(error){fail(error);}finally{e.disabled=false;}};return e;}
function field(container,key,label,value,kind='text',help='',choices=[]){
 const wrap=node('div',undefined,'field'),id='field-'+key.replaceAll('.','-'),l=node('label',label);l.htmlFor=id;
 const input=node(kind==='textarea'||kind==='lines'?'textarea':kind==='select'?'select':'input');input.id=id;
 if(kind==='select'){for(const choice of choices){const o=node('option',choice.label);o.value=choice.id;input.append(o);}}
 else if(kind!=='textarea'&&kind!=='lines')input.type=kind;
 if(kind==='number'){input.min=1;input.max=100;input.step=1;}
 if(readOnlyDemo)input.disabled=true;
 input.value=kind==='lines'?(value||[]).join('\n'):value??'';
 if(key==='web.assistant_name')input.placeholder='例如：客户服务助手';
 if(key==='web.company_name')input.placeholder='填写你的企业或业务场景';
 input.addEventListener('input',()=>{dirty=true;updateAction();});
 input.addEventListener('change',()=>{dirty=true;updateAction();});
 wrap.append(l,input);if(help)wrap.append(node('small',help));$(container).append(wrap);bindings.push({key,input,kind});
}
function renderFields(){
 bindings.length=0;for(const id of ['identity-fields','model-fields','retrieval-fields','answer-fields'])$('#'+id).replaceChildren();
 for(const [key,label,kind] of [['assistant_name','助手名称','text'],['company_name','企业 / 场景名称','text'],['description','场景介绍','textarea'],['welcome_title','欢迎标题','text'],['welcome_subtitle','欢迎说明','text'],['examples','示例问题（每行一条，最多8条）','lines'],['accent','品牌主色','color']])field('#identity-fields','web.'+key,label,state.web[key],kind);
 field('#identity-fields','web.theme','聊天风格',state.web.theme,'select','',[{id:'clean',label:'极简对话'},{id:'soft',label:'柔和陪伴'},{id:'night',label:'深色专注'}]);
 for(const [key,label] of [['answer','回答模型'],['embedding','Embedding 模型']])field('#model-fields','models.'+key,label,state.models[key],'select','当前草稿：'+state.models[key]+'；当前服务：'+(state.current_models?.[key]||'未发布'),state.model_options[key]);
 $('#service-state').textContent=Object.entries({answer:'回答服务',embedding:'向量服务',database:'数据库服务'}).map(([k,v])=>v+'：'+(state.services[k]?'已配置':'未配置，请联系维护人员')).join('；')+'。当前向量维度：'+known(state.models.dimensions)+'。';
 for(const [key,label,help] of [['vector_top_k','向量召回 Top-K','语义检索最多保留多少个候选。默认值由平台配置；范围1–100。'],['keyword_top_k','关键词召回 Top-K','关键词检索最多保留多少个候选。范围1–100。'],['rerank_candidate_limit','融合候选上限','合并检索候选后的数量上限，当前未调用重排序模型。范围1–100。'],['context_top_k','回答上下文 Top-K','最多向回答模型提供多少组证据。范围1–100。']])field('#retrieval-fields','top_k.'+key,label,state.top_k[key],'number',help);
 for(const [key,label] of [['scope','服务范围与边界'],['audience','服务对象'],['tone','回答风格'],['handoff','资料不足与转人工处理']])field('#answer-fields','business.'+key,label,state.business[key],'textarea','独立保存，发布后用于本助手的回答。');
 const logo=state.logo?.logo_asset;$('#logo-preview').hidden=!logo;if(logo)$('#logo-preview').src=logo+'?v='+encodeURIComponent(state.logo.logo_version||state.revision);
 updateAction();
}
function collect(){const payload={revision:state.revision,web:{},top_k:{},business:{},models:{}};
 for(const b of bindings){const [section,key]=b.key.split('.');payload[section][key]=b.kind==='number'?Number(b.input.value):b.kind==='lines'?b.input.value.split('\n').map(x=>x.trim()).filter(Boolean):b.input.value;}
 return payload;
}
async function ensureWorkspace(){
 if(assistantId)return;
 const payload=collect();if(!payload.web.assistant_name.trim()||!payload.web.company_name.trim())throw Error('请先填写助手名称和企业 / 场景名称。');
 const a=await api('/api/admin/assistants',{unified_flow:true,name:payload.web.assistant_name,company:payload.web.company_name,description:payload.web.description,accent:payload.web.accent,theme:payload.web.theme});
 assistantId=a.id;history.replaceState(null,'','/settings?assistant='+encodeURIComponent(assistantId)+'#'+currentStep());
 const fresh=await api(endpoint('/editor'));state={...fresh,web:payload.web,top_k:payload.top_k,business:payload.business,models:{...fresh.models,...payload.models}};renderFields();dirty=true;
}
async function save(){
 if(busy)return;busy=true;updateAction();$('#settings-error').hidden=true;
 try{await ensureWorkspace();state=await api(endpoint('/editor'),collect());dirty=false;renderFields();await refreshDocuments();notice('草稿已保存，尚未发布。');return true;}catch(e){fail(e);return false;}finally{busy=false;updateAction();}
}
function currentStep(){const key=location.hash.slice(1);return steps.includes(key)?key:key==='prompt'?'answer':'identity';}
function openStep(key){if(!steps.includes(key))key='identity';for(const s of steps){$('#tab-'+s).hidden=s!==key;const tab=document.querySelector('[data-tab="'+s+'"]');tab.classList.toggle('selected',s===key);tab.setAttribute('aria-selected',String(s===key));}history.replaceState(null,'','#'+key);$('#configuration-back').hidden=key===steps[0];$('#configuration-next').hidden=key===steps.at(-1);}
for(const tab of document.querySelectorAll('[data-tab]'))tab.onclick=()=>openStep(tab.dataset.tab);
$('#configuration-back').onclick=()=>openStep(steps[Math.max(0,steps.indexOf(currentStep())-1)]);
$('#configuration-next').onclick=()=>openStep(steps[Math.min(4,steps.indexOf(currentStep())+1)]);
window.addEventListener('hashchange',()=>openStep(currentStep()));
$('#settings-form').onsubmit=e=>{e.preventDefault();save();};
window.addEventListener('beforeunload',e=>{if(dirty||queue.results().some(x=>['pending','failed'].includes(x.status))){e.preventDefault();e.returnValue='';}});
function updateAction(){
 if(!state)return;const b=$('#primary-action');b.disabled=busy;
 if(readOnlyDemo){b.textContent='作品演示 · 只读';b.disabled=true;$('#flow-status').textContent='当前为固定知识快照，可查看配置与附件，真实问答请从工作台打开出行通。';return;}
 if(dirty){b.textContent='提交草稿';$('#flow-status').textContent='有未提交修改，当前正式问答仍使用已发布版本。';return;}
 const stage=state.lifecycle.state;
 const text={incomplete:'完善配置后提交',draft:'保存草稿',needs_build:'构建候选版本',building:'正在构建',ready_to_publish:state.lifecycle.enabled?'预览并发布新版':'预览并启用',published:'当前已发布',disabled:'启用当前版本'};
 b.textContent=text[stage]||'保存草稿';b.disabled=busy||['building','published'].includes(stage);
 $('#flow-status').textContent='当前服务：'+(state.knowledge.current_version?'已发布版本':'尚未发布')+'；待发布：'+(state.knowledge.candidate_version?'候选版本已就绪':stage==='building'?'构建中':stage==='needs_build'?'草稿待构建':'无')+'。'+(state.lifecycle.enabled?'旧版继续服务。':'助手尚未对外启用。');
}
$('#primary-action').onclick=async()=>{
 if(dirty||!assistantId){await save();return;}
 try{
  if(state.lifecycle.state==='incomplete'){openStep('knowledge');notice('请上传资料，再提交完整草稿。');await save();return;}
  if(state.lifecycle.state==='needs_build'){
   if(!state.lifecycle.configuration_complete)throw Error('请先上传资料，再保存完整草稿。');
   if(!confirm('构建会使用已保存草稿调用 Embedding 并写入数据库，可能产生费用。确认现在构建？'))return;
   await api('/api/build?assistant='+encodeURIComponent(assistantId),{});await refreshState();await jobStatus();
  }else if(state.lifecycle.state==='ready_to_publish')await previewCandidate();
  else if(state.lifecycle.state==='disabled'){await api(endpoint('/resume'),{});await refreshState();notice('已恢复当前发布版本服务，草稿未被发布。');}
 }catch(e){fail(e);}
};
async function refreshState(){if(!assistantId)return;const fresh=await api(endpoint('/editor'));if(!dirty){state=fresh;updateAction();renderPlan();}else{state.lifecycle=fresh.lifecycle;state.knowledge=fresh.knowledge;updateAction();}}
function renderPlan(){
 const p=state.knowledge,box=$('#knowledge-plan');box.replaceChildren();
 box.append(node('p','当前服务版本：'+(p.current_version||'尚未发布')),node('p','待发布版本：'+(p.candidate_version||'尚未构建')));
 for(const [label,values] of [['新增',p.added||[]],['替换',(p.replaced||[]).map(x=>x.old+' → '+x.new)],['移除',p.removed||[]]])box.append(node('p',label+'：'+(values.join('、')||'无')));
 $('#version-history').replaceChildren();const history=state.history||[];
 if(!history.length)$('#version-history').append(node('p',p.current_version?'当前保留既有发布版本，尚无新版发布记录。':'尚无发布记录。'));
 for(const h of history)$('#version-history').append(node('p',known(h.published_at)+' · '+known(h.version)));
}
async function refreshDocuments(){
 if(!assistantId)return;const data=await api(endpoint('/documents'));documents=data.documents;
 const visible=documents.filter(d=>!d.removed&&!d.deleted);
 $('#document-counts').textContent='共 '+visible.length+' 份附件';
 const box=$('#document-rows');box.replaceChildren();
 if(!visible.length){const row=node('tr'),cell=node('td','还没有附件，请从上方上传。');cell.colSpan=5;row.append(cell);box.append(row);}
 for(const d of visible){
  const row=node('tr'),name=node('td'),chunks=node('td',chunkValue(d.built_chunks)),status=node('td',buildLabel(d)),toggleCell=node('td'),actions=node('td');
  const preview=button(d.attachment_name||d.name||d.title||'未命名附件',()=>showDocument(d.id),'attachment-name');name.append(preview);
  const toggle=node('input');toggle.type='checkbox';toggle.setAttribute('role','switch');toggle.setAttribute('aria-label','启用 '+(d.title||d.name));
  toggle.checked=d.build_state==='built'&&!d.disabled;toggle.disabled=readOnlyDemo||d.build_state!=='built';
  toggle.title=toggle.disabled?(d.build_state==='excluded'?'此资料不允许使用':'构建完成后可启用'):'控制此附件是否用于回答';
  toggle.onchange=async()=>{const enabled=toggle.checked;toggle.disabled=true;try{const result=await api(endpoint('/documents/'+encodeURIComponent(d.id)+'/action'),{action:enabled?'enable':'disable'});notice(result.message);await refreshDocuments();}catch(e){toggle.checked=!enabled;fail(e);}finally{toggle.disabled=readOnlyDemo||d.build_state!=='built';}};
  toggleCell.append(toggle);
  const buttons=node('div',undefined,'attachment-actions');
  const update=button('更新上传',()=>{updatingDocumentId=d.id;$('#replacement-upload').value='';$('#replacement-upload').click();},'text-button');
  update.disabled=readOnlyDemo||d.build_state==='excluded';
  buttons.append(update,button('删除',async()=>{if(!confirm('删除此附件？删除后将立即停止用于回答，历史文件保留。'))return;const result=await api(endpoint('/documents/'+encodeURIComponent(d.id)+'/action'),{action:'delete'});notice(result.message);await refreshDocuments();},'text-button'));
  if(readOnlyDemo)for(const actionButton of buttons.querySelectorAll('button'))actionButton.disabled=true;
  actions.append(buttons);row.append(name,chunks,status,toggleCell,actions);box.append(row);
 }
 await refreshState();
}
let updatingDocumentId=null;
$('#replacement-upload').onchange=async()=>{
 const file=$('#replacement-upload').files[0];if(!file||!updatingDocumentId)return;
 try{const form=new FormData();form.append('file',file);const result=await appRequest(endpoint('/documents/'+encodeURIComponent(updatingDocumentId)+'/update'),{method:'POST',body:form});notice(result.message);await refreshDocuments();}catch(e){fail(e);}finally{$('#replacement-upload').value='';updatingDocumentId=null;}
};
async function showDocument(id){
 reviewing=await api(endpoint('/documents/'+encodeURIComponent(id)));const d=reviewing;
 $('#document-heading').textContent=d.attachment_name||d.title||'附件预览';const metadata=$('#document-metadata');metadata.replaceChildren();
 for(const [label,value] of [['来源',d.source_url||'上传资料，未登记外部链接'],['当前参与检索',d.participating?'是':'否'],['当前版本',d.active_version||'未生效'],['待发布版本',d.candidate_version||'无'],['构建状态',buildLabel(d)],['切片数',chunkValue(d.built_chunks)],['可检索切片数',chunkValue(d.built_retrievable_chunks)],['提取格式',d.format],['提取字符数',d.characters]])metadata.append(node('p',label+'：'+known(value)));
 for(const warning of d.warnings||[])metadata.append(node('p',warning,'notice'));
 $('#document-original').href=d.original_url;$('#review-text').textContent=d.text||'没有提取文本。';
 $('#review-error').textContent='';$('#document-dialog').showModal();
}
$('#close-document').onclick=()=>$('#document-dialog').close();
if(readOnlyDemo){for(const id of ['knowledge-upload','upload-documents','assistant-logo','upload-logo'])$('#'+id).disabled=true;}
const queue=DocumentUploads.create({input:$('#knowledge-upload'),container:$('#document-upload-queue'),summary:$('#document-upload-summary')});
$('#upload-documents').onclick=async()=>{const b=$('#upload-documents');b.disabled=true;try{await ensureWorkspace();const result=await queue.upload(endpoint('/documents'));await refreshDocuments();notice(`已提取 ${result.uploaded} 份，已存在 ${result.existing} 份，失败 ${result.failed} 份。上传成功后可构建。`);b.textContent=result.failed?'重试失败文件':'上传所选资料';}catch(e){fail(e);}finally{b.disabled=false;}};
$('#upload-logo').onclick=async()=>{try{const file=$('#assistant-logo').files[0];if(!file)throw Error('请先选择 Logo。');if(file.size>5*1024*1024)throw Error('Logo 不能超过5MB。');await ensureWorkspace();const data=new FormData();data.append('file',file);data.append('revision',state.revision);const result=await appRequest(endpoint('/logo'),{method:'POST',body:data});state.revision=result.revision;state.logo={logo_asset:result.url,logo_version:result.logo_version};$('#logo-preview').src=result.url;$('#logo-preview').hidden=false;$('#logo-status').textContent='Logo 已保存到草稿；随版本发布。';await refreshState();}catch(e){fail(e);}};
function renderJob(j){
 $('#build-status').textContent=j.message||'尚未开始构建。';$('#build-phases').replaceChildren();
 for(const [key,label] of Object.entries(phases)){const p=j.stages?.[key];$('#build-phases').append(node('li',label+'：'+(!p?'尚未开始':p.state==='failed'?'失败':p.state==='done'?'完成':'处理中')+(p?.total!==null&&p?.total!==undefined?` ${p.completed}/${p.total}`:'')));}
 const metrics=j.metrics||{};$('#build-metrics').replaceChildren();
 for(const [key,label] of [['total_chunks','全部片段总数'],['chunks','可检索片段总数'],['reused_vectors','复用向量数'],['new_vectors','新生成向量数'],['written_rows','本次成功写入数'],['confirmed_rows','已确认入库数'],['failed_rows','失败数'],['index_ready','索引就绪']])$('#build-metrics').append(node('dt',label),node('dd',key==='index_ready'?(metrics[key]?'已就绪':'未确认就绪'):known(metrics[key])));
 if(j.state==='failed')$('#build-status').append(button('重试失败构建',async()=>{if(dirty)throw Error('请先保存草稿。');if(!confirm('重试可能调用 Embedding 并写入数据库，确认继续？'))return;await api('/api/build?assistant='+encodeURIComponent(assistantId),{});await jobStatus();}));
}
async function jobStatus(){if(!assistantId)return;try{const j=await api('/api/build?assistant='+encodeURIComponent(assistantId));renderJob(j);if(j.state==='running'){if(!poll)poll=setInterval(jobStatus,1500);}else if(poll){clearInterval(poll);poll=null;await refreshDocuments();}await refreshState();}catch(e){fail(e);if(poll){clearInterval(poll);poll=null;}}}
async function previewCandidate(){
 const d=await api(endpoint('/preview')),box=$('#candidate-content');box.replaceChildren();box.append(node('p','候选版本：'+d.index_id),node('p','可检索片段：'+d.chunks));
 const list=node('ul');for(const item of d.documents)list.append(node('li',item[1]));box.append(list);
 for(const sample of d.samples){box.append(node('h3',sample.title),node('p',sample.text));}
 $('#candidate-chat-link').href='/admin/assistants/'+encodeURIComponent(assistantId)+'/preview-chat';$('#publish-error').textContent='';$('#candidate-dialog').showModal();
}
$('#confirm-publish').onclick=async()=>{try{if(dirty)throw Error('有未提交修改，请关闭预览并保存、重新构建。');await api(endpoint('/activate'),{});$('#candidate-dialog').close();await refreshDocuments();notice('发布完成，新问题使用新版本。');}catch(e){$('#publish-error').textContent=e.message;}};
$('#close-candidate').onclick=()=>$('#candidate-dialog').close();
if($('#logout'))$('#logout').onclick=async()=>{await api('/logout',{});location.href='/login';};
(async()=>{try{state=await api(assistantId?endpoint('/editor'):'/api/admin/editor-defaults');renderFields();renderPlan();renderJob(state.job);openStep(currentStep());if(assistantId){await refreshDocuments();await jobStatus();}}catch(e){fail(e);}})();
