#!/usr/bin/env python3
import sys,json,re,hashlib,difflib,unicodedata
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
R=Path(__file__).resolve().parents[1]
E=[('validity-current','https://source.nra.gov.cn/jglz/fgzd/bmgz/'),('validity-page2','https://source.nra.gov.cn/jglz/fgzd/bmgz/index_1.shtml'),('2026-news','https://www.rails.cn/html/news/12/2026/20260601_68794.html'),('2026-desktop','https://kyfw.12306.cn/otn/gonggao/saleTicketMeans.html'),('nra-mps-2022-1-pdf','https://source.nra.gov.cn/xxgk/gkml/ztjg/gfzd/gfxw/202205/P020220527503609187824.pdf'),('mot-2023-21-pdf','https://xxgk.mot.gov.cn/2020/jigou/fgs/202401/P020240104346779505397.pdf')]
registry=[]
for id,u in E:
 p=R/'evidence'/(id+('.pdf' if u.endswith('.pdf') else '.html'))
 if '--offline' not in sys.argv:
  r=requests.get(u,headers={'User-Agent':'Mozilla/5.0'},timeout=60);r.raise_for_status();p.write_bytes(r.content)
 registry.append({'path':str(p.relative_to(R)),'url':u,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size})
checks=[]
def norm(t):
 t=unicodedata.normalize('NFKC',t)
 t=re.sub(r'(?m)^\s*[—-]?\s*\d+\s*[—-]?\s*$','',t)
 return ''.join(c for c in t if c.isalnum() and not ('\ua000'<=c<='\ua4ff'))
for p in sorted(list((R/'raw').glob('*.pdf'))+list((R/'evidence').glob('*.pdf'))):
 reader=PdfReader(p);t='\n'.join(page.extract_text() or '' for page in reader.pages)
 (R/'evidence'/(p.stem+'.extracted.txt')).write_text(t)
 id=next(x for x in ['mot-2022-37','mot-2022-39','mot-2023-5','mot-2023-21','nra-mps-2022-1'] if p.stem.startswith(x))
 md=(R/'corpus'/(id+'.md')).read_text();body=md.split('> 原文照录；本文件的标题标记为结构化加工，不改变正文。\n\n')[1]
 body=re.sub(r'^#{2,4} ','',body,flags=re.M)
 # Compare paragraph coverage independent of PDF pagination, spaces, and page footer digits.
 paras=[norm(x) for x in body.splitlines() if x.strip()];nt=norm(t)
 missing=[x for x in paras if x not in nt]
 checks.append({'id':id,'pdf':str(p.relative_to(R)),'pages':len(reader.pages),'paragraphs':len(paras),'alphanumeric_normalized_covered':len(paras)-len(missing),'unmatched_paragraphs':missing,'note':'逐段匹配：NFKC、去独立页码、仅保留字母数字汉字、剔除彝文字形映射标点；不构成标点级一致性证明'})
(R/'evidence/pdf-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
(R/'evidence/source-registry.json').write_text(json.dumps(registry,ensure_ascii=False,indent=2))
for x in checks:print(x['id'],x['pages'],x['alphanumeric_normalized_covered'],'/',x['paragraphs'],'unmatched',x['unmatched_paragraphs'])
