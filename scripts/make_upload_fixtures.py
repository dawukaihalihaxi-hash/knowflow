"""Generate public disposable upload fixtures; optional XLS creation uses xlwt."""
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];folder=ROOT/'tests/fixtures/upload';folder.mkdir(parents=True,exist_ok=True)
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
book=Workbook();sheet=book.active;sheet.title='功能清单';sheet.append(['编号','功能名称','验收条件']);sheet.append(['P01','多文件上传','各文件独立成功或失败']);sheet.append(['P02','人工确认','确认前不能用于回答']);s=book.create_sheet('费用示例');s.append(['项目','数量','示例金额']);s.append(['测试服务',2,50]);s['C3']='=C2*B2';book.save(folder/'验收功能清单.xlsx')
xml='''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>验收PRD：企业知识资料上传功能</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>功能</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>验收标准</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>批量上传</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>一个文件失败不影响其他资料</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>这些内容仅用于程序验收，不是真实业务政策。</w:t></w:r></w:p></w:body></w:document>'''
with zipfile.ZipFile(folder/'验收产品需求.docx','w') as z:
 z.writestr('word/document.xml',xml)
 z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
 z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
writer=PdfWriter();page=writer.add_blank_page(400,400)
font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 350 Td (Upload QA fixture. Review each document before indexing.) Tj ET');page[NameObject('/Contents')]=writer._add_object(stream)
with (folder/'验收说明.pdf').open('wb') as f:writer.write(f)
(folder/'验收产品清单.csv').write_text('产品,说明\n知识助手,支持多份企业资料并逐份审核\n',encoding='utf-8-sig')
(folder/'验收事项.tsv').write_text('步骤\t说明\n预览\t先核对全文再确认\n',encoding='utf-8')
(folder/'损坏示例.pdf').write_bytes(b'not a valid pdf')
try:
 import xlwt
 old=xlwt.Workbook();s=old.add_sheet('历史功能表');s.write(0,0,'名称');s.write(0,1,'说明');s.write(1,0,'旧版Excel');s.write(1,1,'仅读取保存值，不执行宏和公式');old.save(str(folder/'验收旧版表格.xls'))
except ImportError:print('Optional xlwt missing; existing XLS fixture remains unchanged.')
print(folder)
