import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chunk_builder import build, paragraphs
PROJECT = Path(__file__).resolve().parents[1]

class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT/'knowledge', self.root/'knowledge')
        shutil.copytree(PROJECT/'config', self.root/'config')
        self.config = self.root/'config/chunking.railway.json'

    def read(self):
        return [json.loads(x) for x in (self.root/'artifacts/railway-chunks/chunks.jsonl').read_text().splitlines()]

    def test_complete_coverage_and_structures(self):
        report = build(self.config)
        chunks = self.read()
        self.assertEqual(report['excluded_documents'], ['cr-2026-rules'])
        self.assertFalse(any(c['document_id'] == 'cr-2026-rules' for c in chunks))
        self.assertEqual(report['retrieval_candidate_count'], 202)
        for doc in json.loads((self.root/'knowledge/railway/manifest.json').read_text())['documents']:
            if not doc['ingestion_allowed']:
                continue
            source = paragraphs((self.root/'knowledge/railway'/doc['corpus_path']).read_text())
            actual = [p for c in chunks if c['document_id'] == doc['id'] for p in c['paragraphs']]
            self.assertEqual([(p['text'],p['line_start'],p['line_end']) for p in actual],
                             [(p['text'],p['line_start'],p['line_end']) for p in source])
            self.assertEqual([p['source_paragraph_index'] for p in actual], list(range(1,len(source)+1)))
        self.assertEqual([d['articles'] for d in report['documents'] if d['articles']], [52,32,52,37])
        catalog = [c for c in chunks if c['kind']=='catalog_item']
        self.assertEqual(len(catalog), 22)
        power = next(c for c in catalog if '100Wh' in c['body'])
        self.assertIn('限制随身携带', power['context'])
        self.assertIn('电动轮椅除外', power['body'])
        notice = [c for c in chunks if c['document_id']=='moe-cr-2023-4' and c['kind']=='section']
        self.assertEqual(len(notice),6)
        self.assertEqual(len([p for p in notice[-1]['paragraphs'] if p['text'].startswith('（')]),6)
        for c in chunks:
            self.assertTrue(all(ref in {x['chunk_id'] for x in chunks} for ref in c['context_chunk_ids']))

    def test_determinism(self):
        build(self.config)
        p=self.root/'artifacts/railway-chunks/chunks.jsonl'
        before=p.read_bytes()
        build(self.config)
        self.assertEqual(before,p.read_bytes())

    def test_tampering_preserves_previous_output(self):
        build(self.config)
        out=self.root/'artifacts/railway-chunks/chunks.jsonl'
        before=out.read_bytes()
        p=self.root/'knowledge/railway/corpus/mot-2022-37.md'
        p.write_text(p.read_text()+'\n篡改')
        with self.assertRaisesRegex(ValueError,'SHA256 mismatch'):
            build(self.config)
        self.assertEqual(before,out.read_bytes())

    def test_path_escape(self):
        p=self.root/'knowledge/railway/manifest.json'
        m=json.loads(p.read_text());m['documents'][0]['corpus_path']='../outside.md'
        p.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'escapes'):
            build(self.config)

    def test_blocked_document_is_never_opened(self):
        p=self.root/'knowledge/railway/manifest.json'
        m=json.loads(p.read_text())
        for d in m['documents']:
            if not d['ingestion_allowed']:
                d['corpus_path']='../../secret';d['raw_files']=[{'path':'../../secret','sha256':'bad'}]
        p.write_text(json.dumps(m))
        build(self.config)
        self.assertFalse(any(c['document_id']=='cr-2026-rules' for c in self.read()))

    def test_reject_source_overwrite(self):
        cfg=json.loads(self.config.read_text());cfg['output_dir']='knowledge/railway/corpus'
        self.config.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError,'separate'):
            build(self.config)

    def test_changing_strategy_changes_chunk_id(self):
        build(self.config);before=self.read()[-1]['chunk_id']
        cfg=json.loads(self.config.read_text());cfg['document_strategies']['cr-2025-student']='sections'
        self.config.write_text(json.dumps(cfg));build(self.config)
        self.assertNotEqual(before,self.read()[-1]['chunk_id'])

if __name__=='__main__':unittest.main()
