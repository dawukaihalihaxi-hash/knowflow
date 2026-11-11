"""Verify network-failure fallback without model calls or cloud writes."""
import argparse,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from rag_pipeline import Pipeline,ServiceConnectionError
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--repeats',type=int,default=20);a=p.parse_args()
if not 1<=a.repeats<=100:p.error('repeats must be 1–100')
pipeline=Pipeline(a.config);cache=pipeline.cache();first=cache['vectors'].get(pipeline.rows[0]['chunk_id'])
if not first:raise SystemExit('Current complete vector cache is required')
if not pipeline.cfg['database'].get('local_read_fallback'):raise SystemExit('Enable local_read_fallback in settings first')
with patch.object(pipeline,'embed_texts',return_value=[first['vector']]),patch.object(pipeline,'db',side_effect=ServiceConnectionError('simulated network outage')):
 for _ in range(a.repeats):
  result=pipeline.search('retrieval resilience diagnostic')
  assert result['retrieval_backend']=='local_vector_cache'
print(json.dumps({'passed':a.repeats,'network_calls':0,'model_calls':0,'index_id':pipeline.index_id}))
