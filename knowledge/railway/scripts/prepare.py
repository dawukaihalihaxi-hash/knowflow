#!/usr/bin/env python3
"""python prepare.py [--offline]; downloads originals or rebuilds from preserved bytes."""
import argparse,hashlib,json,re,sys,datetime
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
ROOT=Path(__file__).resolve().parents[1]
def sha(b):return hashlib.sha256(b).hexdigest()
def number(s):
 d=dict(zip('零一二三四五六七八九',range(10)));n=0;v=0
 for c in s:
  if c in d:v=d[c]
  elif c=='十':n+=(v or 1)*10;v=0
  elif c=='百':n+=(v or 1)*100;v=0
 return n+v
def get(url,path,offline):
 if not offline:
  r=requests.get(url,headers={'User-Agent':'Mozilla/5.0'},timeout=60);r.raise_for_status();path.write_bytes(r.content)
 b=path.read_bytes();return b

def main():
 offline='--offline' in sys.argv;manifest=[]
 old=json.loads((ROOT/'manifest.json').read_text())['documents'] if (ROOT/'manifest.json').exists() else []
 old_times={x['id']:x['retrieved_at'] for x in old}
 for s in json.loads((ROOT/'scripts/sources.json').read_text()):
  s=s.copy();id=s['id'];p=ROOT/'raw'/f'{id}.html';b=get(s['url'],p,offline)
  # All chosen sites declare UTF-8; preserve bytes independently of decoded text.
  html=b.decode('utf-8-sig');soup=BeautifulSoup(html,'html.parser');files=[p];file_urls={str(p):s['url']}
  if s['selector']=='javascript':
   jsurl=urljoin(s['url'],next(x['src'] for x in soup.select('script[src]') if 'transportationRules.' in x['src']))
   jp=ROOT/'raw'/f'{id}.js';jb=get(jsurl,jp,offline);files.append(jp);s['body_url']=jsurl;file_urls[str(jp)]=jsurl
   literals=re.findall(r'\._v\(("(?:\\.|[^"\\])*")\)',jb.decode('utf-8'))
   lines=[]
   for lit in literals:
    t=json.loads(lit).replace('\\n','\n').strip()
    if t:lines.extend(x.strip() for x in t.splitlines() if x.strip())
  else:
   node=soup.select_one(s['selector']);assert node is not None,(id,'selector missing')
   for x in node.select('script,style'):x.decompose()
   # Preserve paragraphs; inline spans are joined without injecting newlines.
   for block in node.find_all(['p','div','h1','h2','h3','li','br']):
    block.insert_before('\n');block.insert_after('\n')
   lines=[x.strip() for x in node.get_text('').splitlines() if x.strip()]
   lines=[x for x in lines if not re.match(r'^相关(政策)?解读[：:]',x)]
   for a in soup.select('a[href]'):
    if re.search(r'\.(pdf|docx?)(?:$|\?)',a['href'],re.I):
     u=urljoin(s['url'],a['href']);ap=ROOT/'raw'/(id+'-'+u.split('/')[-1].split('?')[0])
     try:get(u,ap,offline);files.append(ap);file_urls[str(ap)]=u
     except Exception as e:s.setdefault('attachment_errors',[]).append({'url':u,'error':str(e)})
  lines=[re.sub(r'[\u00a0\u2002\u2003\u3000\t]+',' ',x).strip() for x in lines]
  nums=[number(m.group(1)) for x in lines if (m:=re.match(r'^第([一二三四五六七八九十百零]+)条',x))]
  expected=s['expected_articles'];seq=bool(nums) and nums==list(range(1,max(nums)+1))
  if expected:assert seq and max(nums)==expected,(id,nums)
  if nums:assert seq,(id,nums)
  md=[f'# {s["title"]}','',f'> 来源：{s["url"]}',f'> 文号：{s["document_number"] or "官方页面未载明，不推测"}',f'> 文本类型：{s["type"]}；有效性：{s["validity"]}','> 原文照录；本文件的标题标记为结构化加工，不改变正文。','']
  for x in lines:
   prefix='## ' if re.match(r'^第[一二三四五六七八九十百]+章',x) else '### ' if re.match(r'^第[一二三四五六七八九十百]+条',x) else '#### ' if re.match(r'^[一二三四五六七八九十]+、',x) else ''
   md.extend([prefix+x,''])
  cp=ROOT/('corpus' if s.get('ingestion_allowed',True) else 'evidence')/f'{id}.md';cp.write_text('\n'.join(md),encoding='utf-8')
  s.update({'retrieved_at':old_times.get(id) if offline else datetime.datetime.now(datetime.timezone.utc).isoformat(),'raw_files':[{'path':str(x.relative_to(ROOT)),'url':file_urls[str(x)],'bytes':x.stat().st_size,'sha256':sha(x.read_bytes())} for x in files],'corpus_path':str(cp.relative_to(ROOT)),'corpus_sha256':sha(cp.read_bytes()),'body_text_sha256':sha('\n'.join(lines).encode()),'quality':{'paragraphs':len(lines),'characters':sum(map(len,lines)),'article_count':len(nums),'articles_contiguous':seq if nums else None,'first_paragraph':lines[0],'last_paragraph':lines[-1]},'tenant_scope':'shared_reference','embedding_status':'not_created'})
  manifest.append(s);print(id,s['quality'])
 (ROOT/'manifest.json').write_text(json.dumps({'schema_version':'1.0','as_of':'2026-11-04','documents':manifest},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
