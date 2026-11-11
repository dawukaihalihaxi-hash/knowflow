#!/usr/bin/env python3
import json,hashlib,re
from pathlib import Path
R=Path(__file__).resolve().parents[1]
M=json.loads((R/'manifest.json').read_text())
for d in M['documents']:
 for f in d['raw_files']:
  assert hashlib.sha256((R/f['path']).read_bytes()).hexdigest()==f['sha256'],f['path']
 assert hashlib.sha256((R/d['corpus_path']).read_bytes()).hexdigest()==d['corpus_sha256'],d['id']
 assert (d['corpus_path'].startswith('corpus/'))==d['ingestion_allowed']
for f in json.loads((R/'evidence/source-registry.json').read_text()):
 assert hashlib.sha256((R/f['path']).read_bytes()).hexdigest()==f['sha256']
a=(R/'corpus/nra-mps-2022-1.md').read_text()
sections=re.split(r'^#### [一二三]、',a,flags=re.M)[1:]
assert len(sections)==3
assert [len(re.findall(r'^（[一二三四五六七八九十]+）',s,flags=re.M)) for s in sections]==[10,5,7]
a=(R/'corpus/moe-cr-2023-4.md').read_text()
assert len(re.findall(r'^#### [一二三四五六]、',a,flags=re.M))==6
assert len(re.findall(r'^（[一二三四五六]）',a,flags=re.M))==6
assert sum(d['ingestion_allowed'] for d in M['documents'])==7
assert len(list((R/'corpus').glob('*.md')))==7
print('PASS: 原文、正文、证据SHA256及入库隔离、目录/通知编号检查通过')
