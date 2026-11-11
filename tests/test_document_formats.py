import io,json,zipfile,unittest
from pathlib import Path
from unittest.mock import patch
from openpyxl import Workbook
from pypdf import PdfWriter
import test_web_app
from assistant_catalog import AssistantCatalog
from document_store import DocumentStore
from document_extractors import extract,extract_document,capabilities
from rag_pipeline import PipelineError
ROOT=Path(__file__).resolve().parents[1];FIX=ROOT/'tests/fixtures/upload'
class DocumentFormats(unittest.TestCase):
 def test_pdf_docx_xlsx_xls_csv_tsv_extract_actual_fixtures(self):
  for name,word in [('验收说明.pdf','Review each'),('验收产品需求.docx','一个文件失败不影响其他资料'),('验收功能清单.xlsx','工作表：功能清单'),('验收旧版表格.xls','旧版Excel'),('验收产品清单.csv','逐份审核'),('验收事项.tsv','先核对全文')]:
   with self.subTest(name=name):self.assertIn(word,extract((FIX/name).read_bytes(),Path(name).suffix))
 def test_word_tables_keep_order_and_column_boundaries(self):
  text=extract((FIX/'验收产品需求.docx').read_bytes(),'.docx')
  self.assertLess(text.index('验收PRD'),text.index('Word表格1'));self.assertLess(text.index('Word表格1'),text.index('不是真实业务政策'))
  self.assertIn('A2="批量上传" | B2="一个文件失败不影响其他资料"',text)
 def test_excel_multiple_sheets_headers_and_formula_are_not_lost(self):
  result=extract_document((FIX/'验收功能清单.xlsx').read_bytes(),'.xlsx');text=result['text']
  self.assertEqual(result['extraction']['sheets'],2);self.assertIn('工作表：费用示例',text)
  self.assertIn('首个非空行参考',text);self.assertIn('=C2*B2',text);self.assertIn('无缓存结果，未计算',text);self.assertTrue(result['warnings'])
 def test_csv_chinese_encoding_and_embedded_newlines(self):
  text=extract('项目,说明\n服务,这是一段中文格式验证内容\n'.encode('gb18030'),'.csv');self.assertIn('中文格式验证',text)
  text=extract('项目,说明\n服务,"第一行文字内容\n第二行文字内容"\n'.encode(),'.csv');self.assertIn('\\n',text)
 def test_empty_tables_and_scanned_pdf_are_not_knowledge(self):
  book=Workbook();book.active.title='这只是很长的空工作表名称';data=io.BytesIO();book.save(data)
  with self.assertRaises(PipelineError):extract(data.getvalue(),'.xlsx')
  writer=PdfWriter();writer.add_blank_page(100,100);data=io.BytesIO();writer.write(data)
  with self.assertRaises(PipelineError):extract(data.getvalue(),'.pdf')
 def test_bad_legacy_encrypted_or_mislabelled_files_fail_clearly(self):
  for suffix,raw in [('.doc',b'legacy'),('.pdf',b'broken'),('.xlsx',b'broken'),('.xls',b'broken'),('.html',b'<html/>')]:
   with self.subTest(suffix=suffix):
    with self.assertRaises(PipelineError):extract(raw,suffix)
 def test_oversized_sheet_and_xml_entities_rejected(self):
  book=Workbook();book.active.cell(10001,1,'extra');data=io.BytesIO();book.save(data)
  with self.assertRaises(PipelineError):extract(data.getvalue(),'.xlsx')
  data=io.BytesIO()
  with zipfile.ZipFile(data,'w') as z:z.writestr('word/document.xml','<!DOCTYPE foo [<!ENTITY x "EXPANDED">]><foo>&x;</foo>')
  with self.assertRaises(PipelineError):extract(data.getvalue(),'.docx')
class UploadRoutes(unittest.TestCase):
 setUp=test_web_app.WebTests.setUp
 csrf=test_web_app.WebTests.csrf
 post=test_web_app.WebTests.post
 def upload(self,aid,name,data):return self.client.post('/api/admin/assistants/'+aid+'/documents',data={'file':(io.BytesIO(data),name)},headers={'X-CSRF-Token':self.csrf()})
 def test_multiple_formats_partial_failure_then_review_and_chunk_no_cloud(self):
  created=self.post('/api/admin/assistants',{'name':'资料验收','company':'测试企业'}).get_json();aid=created['id'];ids=[]
  with patch('rag_pipeline.request',side_effect=AssertionError('No network allowed')):
   for name in ['验收产品需求.docx','损坏示例.pdf','验收功能清单.xlsx','验收说明.pdf','验收旧版表格.xls','验收产品清单.csv','验收事项.tsv']:
    r=self.upload(aid,name,(FIX/name).read_bytes())
    if name=='损坏示例.pdf':self.assertEqual(r.status_code,400)
    else:self.assertEqual(r.status_code,201,r.get_json());self.assertEqual(r.get_json()['status'],'approved');ids.append(r.get_json()['id'])
   store=DocumentStore(AssistantCatalog(self.path),aid);self.assertEqual(len(store.all()),6)
   for did in ids:
    doc=store.preview(did);self.assertTrue(doc['text']);store.approve(did,{'confirmed':True,'text_sha256':doc['text_sha256']})
   path,_=store.prepare_version();text=(path.parent.parent/'artifacts/imported/chunks.jsonl').read_text()
   self.assertIn('工作表：功能清单',text);self.assertIn('Word表格1',text)
  self.assertFalse(AssistantCatalog(self.path).get(aid)['enabled'])
 def test_duplicate_retry_does_not_create_another_document(self):
  aid=AssistantCatalog(self.path).get()['id'];raw=(FIX/'验收说明.pdf').read_bytes();one=self.upload(aid,'same.pdf',raw).get_json();two=self.upload(aid,'same.pdf',raw).get_json()
  self.assertEqual(one['id'],two['id']);self.assertTrue(two['duplicate']);self.assertEqual(len([d for d in DocumentStore(AssistantCatalog(self.path),aid).all() if d.get('origin') not in ('official','quarantined')]),1)
 def test_capabilities_and_visible_document_entry(self):
  caps=self.client.get('/api/admin/document-capabilities').get_json();self.assertEqual(caps['max_batch_files'],20)
  for ext in ('.pdf','.docx','.xlsx','.xls'):self.assertIn(ext,caps['extensions'])
  page=self.client.get('/settings').get_data(as_text=True);self.assertIn('2 知识库管理',page);self.assertIn('document-upload-queue',page);self.assertIn('document-upload.js',page)
