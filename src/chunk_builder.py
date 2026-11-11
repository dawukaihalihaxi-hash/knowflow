"""Deterministic, offline, structure-preserving Markdown chunk builder (stdlib only)."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import tempfile
import os

VERSION = 'structural-v1'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def inside(root, relative):
    p = (root / relative).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes configured root: {relative}')
    return p

def checked(root, relative, expected):
    data = inside(root, relative).read_bytes()
    if digest(data) != expected:
        raise ValueError(f'SHA256 mismatch: {relative}')
    return data

def paragraphs(text):
    """Original line spans plus text with only Markdown heading markers removed."""
    result, buf, start = [], [], None
    for i, line in enumerate(text.splitlines() + [''], 1):
        if line.strip():
            if start is None:
                start = i
            buf.append(line)
        elif buf:
            raw = '\n'.join(buf)
            result.append({'text': re.sub(r'^#{1,6}\s+', '', raw),
                           'line_start': start, 'line_end': i - 1,
                           'heading_level': len(re.match(r'^#+', raw)[0]) if raw.startswith('#') else 0})
            buf, start = [], None
    # Leading title and provenance block are generated corpus metadata, not original body.
    if not result or result[0]['heading_level'] != 1:
        raise ValueError('Expected corpus title')
    result = result[1:]
    while result and result[0]['text'].startswith('>'):
        result.pop(0)
    if not result:
        raise ValueError('Empty corpus body')
    for i, block in enumerate(result, 1):
        block['source_paragraph_index'] = i
    return result

def group(blocks, strategy):
    groups, current, chapter = [], None, None
    for b in blocks:
        t = b['text']
        if b['heading_level'] == 2 and re.match(r'^第.+章', t):
            chapter = t
            # Chapter titles remain covered in a context group; never appended to previous article.
            current = {'kind': 'chapter_context', 'chapter': chapter, 'locator': chapter, 'blocks': []}
            groups.append(current)
        elif strategy == 'articles' and b['heading_level'] == 3 and re.match(r'^第.+条', t):
            current = {'kind': 'article', 'chapter': chapter,
                       'locator': re.match(r'^第.+?条', t)[0], 'blocks': []}
            groups.append(current)
        elif strategy in ('sections', 'catalog') and b['heading_level'] == 4:
            current = {'kind': 'section', 'chapter': None,
                       'locator': re.match(r'^[^、]+、', t)[0], 'section_title': t, 'blocks': []}
            groups.append(current)
        elif strategy == 'catalog' and re.match(r'^（[一二三四五六七八九十]+）', t):
            section = current.get('section_title') if current else None
            current = {'kind': 'catalog_item', 'chapter': None,
                       'section_title': section,
                       'locator': (section or '') + '/' + re.match(r'^（.+?）', t)[0], 'blocks': []}
            groups.append(current)
        if current is None:
            current = {'kind': 'document' if strategy == 'whole' else 'preamble',
                       'chapter': None, 'locator': 'preamble', 'blocks': []}
            groups.append(current)
        current['blocks'].append(b)
    return groups

def build(config_path):
    config_path = Path(config_path).resolve()
    cfg = json.loads(config_path.read_text())
    project = config_path.parent.parent
    root = inside(project, cfg['knowledge_root'])
    output = inside(project, cfg['output_dir'])
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError('Output must be separate from knowledge source')
    manifest_bytes = inside(root, cfg['manifest']).read_bytes()
    docs = json.loads(manifest_bytes)['documents']
    if len({d['id'] for d in docs}) != len(docs):
        raise ValueError('Duplicate document IDs')
    chunks, summary, skipped = [], [], []
    for d in docs:
        if d.get('ingestion_allowed') is not True:
            skipped.append(d['id'])
            continue
        strategy = cfg['document_strategies'].get(d['id'], cfg['default_strategy'])
        if strategy not in ('articles', 'catalog', 'sections', 'whole'):
            raise ValueError('Unknown structure strategy')
        for f in d['raw_files']:
            checked(root, f['path'], f['sha256'])
        data = checked(root, d['corpus_path'], d['corpus_sha256'])
        blocks = paragraphs(data.decode('utf-8'))
        groups = group(blocks, strategy)
        expected = d.get('expected_articles')
        article_count = sum(g['kind'] == 'article' for g in groups)
        if expected and article_count != expected:
            raise ValueError(f'Article coverage mismatch: {d["id"]}')
        covered = []
        context_ids = []
        count_before = len(chunks)
        for index, g in enumerate(groups):
            records, item, subitem = [], None, None
            for i, b in enumerate(g['blocks'], 1):
                m = re.match(r'^(（[^）]+）|\d+[.．、])', b['text'])
                if m:
                    if m[0].startswith('（'):
                        item, subitem = m[0], None
                    else:
                        subitem = m[0]
                records.append({**b, 'paragraph_index': i, 'item_path': [v for v in (item, subitem) if v]})
                covered.append((b['line_start'], b['line_end']))
            text = '\n\n'.join(b['text'] for b in g['blocks'])
            identity = {'document': d['id'], 'source': d['corpus_sha256'],
                        'strategy': strategy, 'builder': VERSION, 'ordinal': index}
            cid = digest(json.dumps(identity, sort_keys=True).encode())
            labels = [d['title'], g['chapter'], g.get('section_title')]
            context = '\n'.join(dict.fromkeys(x for x in labels if x))
            c = {'chunk_id': cid, 'document_id': d['id'],
                 'source_version_sha256': d['corpus_sha256'],
                 'raw_source_sha256': sorted(set(f['sha256'] for f in d['raw_files'])),
                 'title': d['title'], 'source_url': d['url'], 'issuer': d['issuer'],
                 'document_number': d['document_number'], 'rule_type': d['type'],
                 'issued_date': d.get('issued_date'), 'published_date': d.get('published_date'),
                 'effective_date': d.get('effective_date'), 'validity_note': d['validity'],
                 'ingestion_allowed': True, 'corpus_path': d['corpus_path'],
                 'retrieval_eligible': g['kind'] not in ('preamble', 'chapter_context') and not (strategy == 'catalog' and g['kind'] == 'section'),
                 'kind': g['kind'], 'chapter': g['chapter'], 'locator': g['locator'],
                 'context': context, 'context_chunk_ids': list(context_ids),
                 'body': text, 'paragraphs': records,
                 'embedding_text': context + '\n\n' + text,
                 'length_characters': len(text), 'oversized': len(text) > cfg['chunking']['review_above_characters'],
                 'builder_version': VERSION, 'embedding_status': 'not_created'}
            # Historical statements are preserved; applicability is not inferred from publication order.
            c['usage_note'] = cfg.get('document_usage_notes', {}).get(d['id'])
            chunks.append(c)
            if g['kind'] == 'preamble':
                context_ids.append(cid)
        assert covered == [(b['line_start'], b['line_end']) for b in blocks], 'Lost/repeated paragraphs'
        summary.append({'document_id': d['id'], 'strategy': strategy,
                        'chunks': len(chunks)-count_before, 'articles': article_count,
                        'source_paragraphs': len(blocks), 'covered_once': True})
    payload = ''.join(json.dumps(c, ensure_ascii=False, sort_keys=True)+'\n' for c in chunks)
    report = {'builder_version': VERSION, 'manifest_sha256': digest(manifest_bytes),
              'config_sha256': digest(config_path.read_bytes()), 'chunk_count': len(chunks),
              'documents': summary, 'excluded_documents': skipped,
              'retrieval_candidate_count': sum(c['retrieval_eligible'] for c in chunks),
              'context_only_count': sum(not c['retrieval_eligible'] for c in chunks),
              'kinds': dict(Counter(c['kind'] for c in chunks)),
              'oversized_chunks': [c['chunk_id'] for c in chunks if c['oversized']],
              'chunks_sha256': digest(payload.encode()), 'embedding_created': False,
              'notes': ['Complete articles retained, no truncation or fixed-length overlap.',
                        'Character review threshold is not a tokenizer limit.',
                        'Source paragraph coverage excludes generated Markdown title/provenance only.',
                        'No tenant authorization or retrieval system implemented.']}
    # All validation precedes writes. Individual files are replaced atomically.
    output.mkdir(parents=True, exist_ok=True)
    samples = '\n\n---\n\n'.join(f'## {c["document_id"]} · {c["locator"]}\n\n{c["context"]}\n\n{c["body"]}' for c in chunks if c['kind'] not in ('preamble','chapter_context'))
    for name, content in [('chunks.jsonl', payload), ('chunk-report.json', json.dumps(report, ensure_ascii=False, indent=2)), ('chunk-preview.md', samples)]:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output, delete=False) as f:
            f.write(content)
            tmp = f.name
        os.replace(tmp, output/name)
    return report

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    result = build(args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
