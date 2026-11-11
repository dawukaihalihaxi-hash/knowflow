"""Local document intake, explicit review, versioned chunk outputs and safe activation."""
import hashlib,io,json,re,secrets,zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
from rag_pipeline import Pipeline,PipelineError,sha,validate_vector,safe_path
from settings_store import atomic,LOCK,SettingsStore
from assistant_catalog import clone_config,read,write

from document_extractors import extract,extract_document,MAX_BYTES,capabilities

class DocumentStore:
    def __init__(self,catalog,aid):
        self.catalog=catalog;self.aid=aid;catalog.get(aid)
        self.root=catalog.root/'local-data/assistants'/aid/'documents';self.meta=self.root/'documents.json'
    def all(self):
        with LOCK:return self._all()
    def _all(self):
        existing=read(self.meta) if self.meta.exists() else []
        record=self.catalog.get(self.aid)
        pipeline=Pipeline(self.catalog.path(record),allow_empty=True)
        groups={}
        sources=[pipeline]
        if record.get('active_path'):sources.append(Pipeline(self.catalog.path(record,active=True),allow_empty=True))
        for source in sources:
            for row in source.context.values():
                bucket=groups.setdefault(row['document_id'],{})
                bucket[row['chunk_id']]=row
        groups={did:list(rows.values()) for did,rows in groups.items()}
        docs=list(existing)
        for did,rows in groups.items():
            if did.startswith('upload-') or any(d['id']==did for d in docs):continue
            text='\n\n'.join(x['body'] for x in rows)
            atomic(self.root/(did+'.txt'),text,private=True)
            docs.append({'id':did,'document_id':did,'origin':'official','name':rows[0]['title'],
              'title':rows[0]['title'],'status':'indexed','text_sha256':sha(text.encode()),
              'characters':len(text),'source_url':rows[0].get('source_url'),'issued_date':rows[0].get('issued_date')})
        profiles=read(self.catalog.root/'config/knowledge-profiles.json')
        profile=next((x for x in profiles if x['id']==pipeline.cfg['web']['knowledge_profile']),{})
        if profile.get('knowledge_root'):
            manifest=safe_path(self.catalog.root,profile['knowledge_root'])/'manifest.json'
            if manifest.exists():
                for item in read(manifest)['documents']:
                    if item.get('ingestion_allowed') or any(d['id']==item['id'] for d in docs):continue
                    text='隔离候选资料，不允许入库或检索；请通过来源原文核对。'
                    atomic(self.root/(item['id']+'.txt'),text,private=True)
                    docs.append({'id':item['id'],'origin':'quarantined','document_id':item['id'],'name':item['title'],'title':item['title'],'status':'quarantined','source_url':item.get('url'),'text_sha256':sha(text.encode()),'characters':0})
        if docs!=existing:self.save(docs)
        return docs
    def change(self,did,action,replacement=None):
        with LOCK:
            rows=self.all();doc=self.get(did)
            allowed=next(d['allowed_actions'] for d in self.listing() if d['id']==did)
            if action not in allowed:raise PipelineError('此资料当前状态不支持该操作，请刷新资料列表。')
            if doc['status']=='quarantined' and action!='delete':raise PipelineError('此资料不允许启用或构建。')
            if action=='delete':doc.update(removed=True,deleted=True,disabled=True)
            elif action=='disable':doc.update(disabled=True)
            elif action=='enable':doc.update(disabled=False)
            elif action=='remove':doc.update(removed=True)
            elif action=='restore':doc.update(removed=False,deleted=False,disabled=False)
            elif action=='replace':
                new=self.get(replacement)
                if new['status'] not in ('approved','indexed','needs_review') or new.get('removed'):raise PipelineError('请选择解析成功的新文件。')
                if replacement==did:raise PipelineError('不能用自身替代。')
                new['replaces']=did
                rows=[new if x['id']==replacement else x for x in rows]
                doc.update(removed=True)
            else:raise PipelineError('未知资料操作。')
            doc['updated_at']=datetime.now(timezone.utc).isoformat()
            rows=[doc if x['id']==did else x for x in rows]
            if action in ('enable','disable'):
                write(self.meta,rows)
                record=self.catalog.get(self.aid)
                if record.get('candidate_path'):self.catalog.update(self.aid,build_signature=SettingsStore(self.catalog.path(record)).revision()+self.revision())
            else:self.save(rows)
            messages={'delete':'资料已删除并停止使用，历史文件保留。','remove':'已加入待发布移除清单，当前版本继续服务。','disable':'资料已立即停用，后续问答不再召回。','enable':'已解除立即停用。','replace':'新版本已关联，构建并发布后生效。','restore':'资料已恢复到草稿清单。'}
            return {**doc,'ok':True,'message':messages[action]}
    def save(self,rows):
        write(self.meta,rows)
        self.catalog.invalidate_candidate(self.aid,'资料清单或审核状态已变化。')
    def revision(self):return sha(json.dumps(self.all(),sort_keys=True).encode())
    def get(self,did):
        doc=next((d for d in self.all() if d['id']==did),None)
        if not doc:raise PipelineError('找不到这个文档。')
        return doc
    def upload(self,name,content):
        name=name.replace('\\','/').split('/')[-1][:180];suffix=Path(name).suffix.lower()
        if not name:raise PipelineError('请选择文件。')
        fingerprint=sha(content)
        with LOCK:
            existing=next((d for d in self.all() if d['name']==name and d.get('raw_sha256')==fingerprint),None)
            if existing:return {**existing,'duplicate':True}
        parsed=extract_document(content,suffix);text=parsed['text']
        with LOCK:
            rows=self.all();existing=next((d for d in rows if d['name']==name and d.get('raw_sha256')==fingerprint),None)
            if existing:return {**existing,'duplicate':True}
            did=secrets.token_hex(10);raw=self.root/(did+suffix)
            raw.parent.mkdir(parents=True,exist_ok=True,mode=0o700);raw.write_bytes(content);raw.chmod(0o600)
            atomic(self.root/(did+'.txt'),text,private=True)
            doc={'id':did,'name':name,'title':Path(name).stem,'bytes':len(content),'raw_sha256':fingerprint,'text_sha256':sha(text.encode()),
                 'format':parsed['format'],'warnings':parsed['warnings'],'extraction':parsed['extraction'],
                 'characters':len(text),'status':'approved','source_url':None,'issued_date':None,
                 'uploaded_at':datetime.now(timezone.utc).isoformat(),
                 'notice':'已提取文字，构建完成后才能用于检索。图片及复杂表格可能需要另行处理。'}
            rows.append(doc);self.save(rows);return doc
    def update_file(self,did,name,content):
        with LOCK:
            old=self.get(did)
            if old.get('removed') or old['status']=='quarantined':raise PipelineError('当前资料不可更新。')
            new=self.upload(name,content)
            if new['id']==did:return {**new,'message':'文件内容未变化，无需重新构建。'}
            if new.get('removed'):raise PipelineError('相同文件已删除，请选择新的文件。')
            self.change(did,'replace',new['id'])
            return {**new,'message':'新文件已上传，等待构建；当前版本继续使用原文件。'}
    def listing(self):
        docs=self.all();record=self.catalog.get(self.aid)
        def counts(key):
            if not record.get(key):return {},{}
            p=Pipeline(self.catalog.root/record[key],allow_empty=True);total={};eligible={}
            for row in p.context.values():total[row['document_id']]=total.get(row['document_id'],0)+1
            for row in p.rows:eligible[row['document_id']]=eligible.get(row['document_id'],0)+1
            return total,eligible
        totals,active=counts('active_path');pending_totals,candidate=counts('candidate_path')
        out=[]
        for d in docs:
            did=d.get('document_id','upload-'+d['id']);live=bool(active.get(did))
            if d.get('removed'):actions=[] if d['status']=='quarantined' else ['restore']
            elif d['status']=='quarantined':actions=['delete']
            elif live:actions=['delete','remove','replace','enable' if d.get('disabled') else 'disable']
            else:actions=['delete']+(['replace'] if d['status'] in ('approved','indexed','needs_review') else [])
            built_in_candidate=did in pending_totals
            built_total=pending_totals.get(did) if built_in_candidate else totals.get(did)
            built_eligible=candidate.get(did,0) if built_in_candidate else active.get(did,0)
            build_state='excluded' if d['status']=='quarantined' else 'built' if built_total is not None else 'pending'
            if not live and build_state=='built' and not d.get('removed'):actions.append('enable' if d.get('disabled') else 'disable')
            out.append({**d,'attachment_name':d['title']+'.txt' if d.get('origin') in ('official','quarantined') else d.get('name',d['title']),'build_state':build_state,'built_chunks':built_total if build_state=='built' else None,
              'built_retrievable_chunks':built_eligible if build_state=='built' else None,
              'built_version':(record.get('candidate_index') if built_in_candidate else record.get('active_index')) if build_state=='built' else None,
              'origin':d.get('origin','upload'),'format':d.get('format','来源文本' if d.get('origin')=='official' else '待核对'),
              'characters':d.get('characters'),'review_status':d['status'],
              'participating':live and record.get('enabled',False) and not d.get('disabled'),
              'active_version':record.get('active_index') if live else None,'active_chunks':active.get(did,0),'total_chunks':totals.get(did,0),
              'candidate_version':record.get('candidate_index') if candidate.get(did) else None,'candidate_chunks':candidate.get(did,0),
              'candidate_total_chunks':pending_totals.get(did,0),'allowed_actions':actions,
              'original_url':f'/api/admin/assistants/{self.aid}/documents/{d["id"]}/original'})
        return out
    def plan(self):
        rows=self.listing();selected=[d for d in rows if d['status'] in ('approved','indexed','needs_review') and not d.get('removed')]
        current={d['id']:d for d in rows if d['active_version']}
        chosen={d['id']:d for d in selected}
        replacements=[{'old':next((x['title'] for x in rows if x['id']==d.get('replaces')),'历史资料'),'new':d['title']} for d in selected if d.get('replaces') in current]
        return {'current_version':self.catalog.get(self.aid).get('active_index'),
          'candidate_version':self.catalog.get(self.aid).get('candidate_index'),
          'added':[d['title'] for d in selected if d['id'] not in current and not d.get('replaces')],
          'removed':[d['title'] for did,d in current.items() if did not in chosen and not any(x.get('replaces')==did for x in selected)],
          'replaced':replacements,'selected_documents':len(selected),'active_documents':len(current),
          'quarantined_documents':sum(d['status']=='quarantined' for d in rows),
          'awaiting_review':sum(d['status']=='needs_review' and not d.get('removed') for d in rows)}
    def preview(self,did):
        doc=self.get(did);text=(self.root/(did+'.txt')).read_text()
        if sha(text.encode())!=doc['text_sha256']:raise PipelineError('提取文本指纹不一致。')
        details=next(d for d in self.listing() if d['id']==did)
        return {**details,'text':text}
    def approve(self,did,values):
        with LOCK:
            doc=self.preview(did)
            if doc['status'] in ('indexed','quarantined') or doc.get('active_version'):raise PipelineError('已入库文档不可原地修改，请上传新版本。')
            if values.get('text_sha256')!=doc['text_sha256'] or values.get('confirmed') is not True:raise PipelineError('请确认本次提取文本后再批准。')
            title=values.get('title',doc['title']).strip()
            if not 1<=len(title)<=180:raise PipelineError('文档标题无效。')
            url=values.get('source_url') or None
            if url and (len(url)>2000 or not url.startswith('https://')):raise PipelineError('来源链接需为HTTPS地址，内部资料可留空。')
            date=values.get('issued_date') or None
            if date:
                try:__import__('datetime').date.fromisoformat(date)
                except ValueError:raise PipelineError('文档日期格式无效。')
            rows=self.all()
            for r in rows:
                if r['id']==did:r.update(status='approved',title=title,source_url=url,issued_date=date,reviewed_at=datetime.now(timezone.utc).isoformat())
            self.save(rows);return self.get(did)
    def prepare_version(self,progress=None):
        with LOCK:
            record=self.catalog.get(self.aid);approved=[d for d in self.all() if d['status'] in ('approved','indexed','needs_review') and d.get('origin','upload')=='upload' and not d.get('removed')]
            if not approved and not self.all():raise PipelineError('没有可构建的资料。')
            selected=[d for d in self.all() if d['status'] in ('approved','indexed','needs_review') and not d.get('removed')]
            if not selected:raise PipelineError('没有可构建的附件，请先上传文件。')
            for i,doc in enumerate(selected):
                self.preview(doc['id'])
                if progress:progress('parsing',i+1,len(selected))
            version=secrets.token_hex(8);target=self.catalog.root/'local-data/assistants'/self.aid/'versions'/version
            path=clone_config(self.catalog.path(record),target);c=read(path)
            base=Pipeline(path,allow_empty=True)
            try:
                cache_source=Pipeline(self.catalog.path(record,active=True),allow_empty=True) if record.get('active_path') else base
                previous_cache=cache_source.cache()
            except PipelineError:previous_cache=None
            excluded={d.get('document_id','upload-'+d['id']) for d in self.all() if d.get('removed')}
            rows=[x for x in base.context.values() if x['document_id'] not in excluded];all_ids={x['document_id'] for x in rows}
            for doc in approved:
                did=doc['id'];document_id='upload-'+did
                if document_id in all_ids:continue
                preview=self.preview(did);text=preview['text'];parts=[p.strip() for p in re.split(r'\n\s*\n',text) if p.strip()]
                if __import__('os').environ.get('APP_PUBLIC_TRIAL')=='1':
                    size=read(self.catalog.root/'config/trial-limits.json')['chunk_characters']
                    parts=[part[i:i+size] for part in parts for i in range(0,len(part),size)]
                canonical=target/'knowledge/uploads'/f'{did}.txt';canonical.parent.mkdir(parents=True,exist_ok=True);canonical.write_text(text)
                chunk_ids=[sha((self.aid+document_id+doc['text_sha256']+str(i)).encode()) for i in range(len(parts))]
                for i,body in enumerate(parts):
                    cid=chunk_ids[i]
                    context_ids=list(dict.fromkeys([chunk_ids[0],chunk_ids[i-1]])) if i>0 else []
                    rows.append({'chunk_id':cid,'document_id':document_id,'source_version_sha256':doc['text_sha256'],'raw_source_sha256':[doc['raw_sha256']],
                       'title':doc['title'],'source_url':doc['source_url'] or f'/a/{self.aid}/sources/{did}',
                       'issuer':'企业上传资料','document_number':None,'rule_type':'enterprise_upload','issued_date':doc['issued_date'],'published_date':None,'effective_date':None,
                       'validity_note':'系统已提取文件文字；不代表人工核验完整性、法律效力或最新有效性。','usage_note':'企业上传来源；若与其他资料冲突，应说明差异，不自行裁决。',
                       'ingestion_allowed':True,'retrieval_eligible':True,'kind':'paragraph','chapter':None,'locator':f'第{i+1}段',
                       'body':body,'embedding_text':doc['title']+'\n\n'+body,'context_chunk_ids':context_ids,
                       'length_characters':len(body),'oversized':len(body)>2000,'embedding_status':'not_created'})
            raw=''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in rows)
            out=target/'artifacts/imported';out.mkdir(parents=True,exist_ok=True)
            atomic(out/'chunks.jsonl',raw);write(out/'chunk-report.json',{'chunks_sha256':sha(raw.encode()),'chunk_count':len(rows),'documents_added':[d['id'] for d in approved],
              'paragraph_coverage':'各提取段落完整保留，不按固定字符截断；不证明PDF表格/图片提取完整。'})
            c.update(chunks_path='artifacts/imported/chunks.jsonl',chunk_report_path='artifacts/imported/chunk-report.json')
            c['web']['knowledge_profile']='uploaded-'+version
            write(target/'config/knowledge-profiles.json',[{'id':c['web']['knowledge_profile'],'name':'企业知识版本 '+version[:6],
                'chunks_path':c['chunks_path'],'chunk_report_path':c['chunk_report_path'],'snapshot_date':datetime.now().date().isoformat()}])
            write(path,c)
            candidate=Pipeline(path)
            if progress:progress('chunking',len(candidate.context),len(candidate.context))
            c['vector_cache_path']='local-data/indexes/'+candidate.index_id+'.json';write(path,c)
            candidate=Pipeline(path)
            if previous_cache and previous_cache.get('profile')==candidate.profile:
                reused={}
                for row in candidate.rows:
                    item=previous_cache['vectors'].get(row['chunk_id'])
                    if item and item.get('input_sha256')==sha(row['embedding_text'].encode()):
                        validate_vector(item['vector'],candidate.profile['dimensions']);reused[row['chunk_id']]=item
                candidate.save_cache({'index_id':candidate.index_id,'profile':candidate.profile,'vectors':reused})
            return path,[d['id'] for d in approved]
    def indexed(self,ids):
        with LOCK:
            rows=self.all()
            for d in rows:
                if d['id'] in ids:d['status']='indexed'
            self.save(rows)
