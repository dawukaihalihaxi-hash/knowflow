"""Run actual configured RAG answers; checks are diagnostics, never a semantic score."""
import argparse,json,sys,time
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R/'src'))
from rag_pipeline import Pipeline,PipelineError,read_env
from answer_service import AnswerService
p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--output',required=True);p.add_argument('--ids',nargs='*');args=p.parse_args()
read_env(R/'.env');output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
consecutive_errors=0
for line in (R/'evals/questions.jsonl').read_text().splitlines():
 case=json.loads(line)
 if args.ids and case['id'] not in args.ids:continue
 if case['id']=='RAG-024':
  result={'id':case['id'],'status':'architecture_test_separate'}
 else:
  start=time.monotonic()
  try:
   response=AnswerService(Pipeline(args.config)).answer(case['question'])
   expected=[s for s in case['expected_sources'] if s in {r['document_id'] for r in Pipeline(args.config).rows}]
   consecutive_errors=0
   cited={x['document_id'] for x in response['sources']}
   result={'id':case['id'],'question':case['question'],'acceptance':case['acceptance'],
           'response':response,'expected_source_coverage':all(x in cited for x in expected) if expected else None,
           'semantic_review':'pending','seconds':round(time.monotonic()-start,2)}
  except PipelineError as e:
   consecutive_errors+=1
   result={'id':case['id'],'status':'request_failed','error':str(e),'semantic_review':'not_run'}
 with output.open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
 print(case['id'],result.get('status',result.get('response',{}).get('status')),flush=True)

 if consecutive_errors>=3:
  print('连续3次请求失败，停止批量评测；请先修复服务状态。',flush=True)
  break
