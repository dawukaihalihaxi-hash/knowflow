"""Private bounded request traces, stored separately for each assistant."""
import json,re,uuid
from datetime import datetime,timezone
from settings_store import atomic,LOCK
from rag_pipeline import PipelineError

def directory(root,aid):
    if not re.fullmatch(r'[a-z0-9-]{2,60}',aid):raise PipelineError('调试助手标识无效。')
    return root/'local-data/answer-traces'/aid

def save_trace(root,aid,trace,limit=20):
    with LOCK:
        folder=directory(root,aid);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        tid=uuid.uuid4().hex
        atomic(folder/(tid+'.json'),json.dumps({'id':tid,'created_at':datetime.now(timezone.utc).isoformat(),**trace},ensure_ascii=False,indent=2),private=True)
        for path in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[limit:]:path.unlink()
        return tid

def load_trace(root,aid,tid):
    with LOCK:
        if not re.fullmatch(r'[0-9a-f]{32}',tid):raise PipelineError('调试记录标识无效。')
        path=directory(root,aid)/(tid+'.json')
        if not path.exists():raise PipelineError('调试记录不存在或已过期。')
        return json.loads(path.read_text())

def list_traces(root,aid):
    with LOCK:
        folder=directory(root,aid);items=[]
        for path in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            d=json.loads(path.read_text());items.append({'id':d['id'],'created_at':d['created_at'],'question':d.get('question',''),'status':d.get('result',{}).get('status','error')})
        return items
