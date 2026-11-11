"""Configured query expansion and approved-source overview assembly; no domain vocabulary."""
import hashlib
import json

def validate_rules(rules):
    if not isinstance(rules,list) or len(rules)>20:raise ValueError('检索意图规则最多20条。')
    ids=set()
    for rule in rules:
        if not isinstance(rule,dict) or set(rule)!={'id','triggers','expansions','overview_markers','exclude_terms','groups'}:raise ValueError('检索意图规则字段无效。')
        if not isinstance(rule['id'],str) or not 1<=len(rule['id'])<=100 or rule['id'] in ids:raise ValueError('检索意图规则标识无效或重复。')
        ids.add(rule['id'])
        for key in ('triggers','expansions','overview_markers','exclude_terms'):
            values=rule[key]
            if not isinstance(values,list) or len(values)>100 or not all(isinstance(x,str) and 0<len(x)<=200 for x in values):raise ValueError('检索意图词表无效。')
        if not rule['triggers'] or not rule['expansions']:raise ValueError('触发词和扩展词不能为空。')
        if not isinstance(rule['groups'],list) or not 1<=len(rule['groups'])<=10:raise ValueError('概览分类数量无效。')
        for g in rule['groups']:
            if not isinstance(g,dict) or set(g)!={'document_id','locator_prefix'} or not all(isinstance(x,str) and 0<len(x)<=300 for x in g.values()):raise ValueError('概览分类选择器无效。')

def plan_query(query,rules):
    matched=[];groups=[];expansions=[]
    compact=''.join(c for c in query.lower() if c.isalnum())
    for rule in rules:
        if not any(t in query for t in rule['triggers']) or any(t in query for t in rule['exclude_terms']):continue
        overview=any(t in query for t in rule['overview_markers']) or compact in {''.join(c for c in t.lower() if c.isalnum()) for t in rule['triggers']}
        if not overview:continue
        matched.append(rule['id']);expansions.extend(rule['expansions']);groups.extend(rule['groups'])
    expansions=list(dict.fromkeys(expansions));groups=list({(g['document_id'],g['locator_prefix']):g for g in groups}.values())
    return {'original_query':query,'search_query':query+(' '+ ' '.join(expansions) if expansions else ''),'matched_rules':matched,'expansions':expansions,'overview_groups':groups}

def assemble_overviews(rows,groups):
    packs=[];missing=[]
    for group in groups:
        prefix=group['locator_prefix'];doc=group['document_id']
        members=[r for r in rows if r['document_id']==doc and (r['locator']==prefix or r['locator'].startswith(prefix+'/'))]
        if not members:missing.append(group);continue
        versions={r['source_version_sha256'] for r in members}
        if len(versions)!=1 or any(r.get('ingestion_allowed') is not True or r.get('retrieval_eligible') is not True for r in members):raise ValueError('概览证据来源或版本不一致。')
        ids=[r['chunk_id'] for r in members];cid='overview-'+hashlib.sha256(json.dumps(ids,ensure_ascii=False).encode()).hexdigest()
        body='\n\n'.join(r['body'] for r in members)
        packs.append({**members[0],'chunk_id':cid,'body':body,'embedding_text':members[0]['title']+'\n'+prefix+'\n\n'+body,'locator':prefix,'context_chunk_ids':[],'source_chunk_ids':ids,'derived_overview':True})
    return packs,missing
