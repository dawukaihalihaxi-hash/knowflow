"""Bounded local text/table extraction; no Office execution or external fetches."""
import csv,io,json,zipfile
from datetime import date,datetime,time
from pathlib import Path
from defusedxml import ElementTree as ET
from rag_pipeline import PipelineError

MAX_BYTES=10*1024*1024
MAX_CHARACTERS=300000
MAX_BATCH_FILES=20
csv.field_size_limit(MAX_CHARACTERS)
SUPPORTED=('.pdf','.docx','.xlsx','.xls','.csv','.tsv','.txt','.md')

def capabilities():
    return {'extensions':list(SUPPORTED),'max_bytes':MAX_BYTES,'max_batch_files':MAX_BATCH_FILES,
            'description':'PDF（文字版）、Word（DOCX）、Excel（XLSX / XLS）、CSV、TSV、TXT、Markdown',
            'limitations':'扫描PDF、图片与图表不做OCR；旧版DOC请先另存为DOCX；加密文件需先解密。'}

def checked_archive(content):
    z=zipfile.ZipFile(io.BytesIO(content))
    if len(z.infolist())>2000 or sum(x.file_size for x in z.infolist())>30*1024*1024:
        z.close();raise PipelineError('Office文件解压大小或内部文件数量超过限制，请拆分。')
    if any(x.flag_bits&1 for x in z.infolist()):z.close();raise PipelineError('暂不支持加密Office文件，请先解密。')
    return z

def cell_value(value):
    if isinstance(value,(datetime,date,time)):return value.isoformat()
    return str(value).replace('\r\n','\n').replace('\r','\n') if value is not None else ''

def column_name(index):
    name=''
    while index:index,rem=divmod(index-1,26);name=chr(65+rem)+name
    return name

class Collector:
    def __init__(self):self.parts=[];self.characters=0;self.data_characters=0
    def add(self,text):
        if not text.strip():return
        self.characters+=len(text)+2
        if self.characters>MAX_CHARACTERS:raise PipelineError('提取文字超过30万字符，请拆分文档。')
        self.parts.append(text)
    def text(self):return '\n\n'.join(self.parts)

def table_rows(collector,name,rows):
    first=None;count=0;cells=0
    for number,values in rows:
        count+=1;cells+=len(values)
        if count>10000 or cells>100000 or len(values)>256:raise PipelineError('表格超过1万行、256列或10万单元格，请拆分。')
        collector.data_characters+=sum(len(cell_value(v)) for v in values if v is not None)
        entries=[f'{column_name(i+1)}{number}='+json.dumps(cell_value(v),ensure_ascii=False) for i,v in enumerate(values) if v is not None and cell_value(v)!='']
        if not entries:continue
        line=' | '.join(entries)
        if first is None:first=line
        reference='' if line==first else '\n首个非空行参考（不自动认定为表头）：'+first
        collector.add(f'{name} · 第{number}行{reference}\n'+line)
    return count

def docx_text(content,collector):
    with checked_archive(content) as z:node=ET.fromstring(z.read('word/document.xml'))
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'};tag='{'+ns['w']+'}'
    body=node.find('w:body',ns)
    if body is None:raise PipelineError('Word文件缺少正文。')
    def paragraph(p):
        return ''.join(n.text or '' if n.tag==tag+'t' else '\t' if n.tag==tag+'tab' else '\n' if n.tag in (tag+'br',tag+'cr') else '' for n in p.iter())
    tables=0
    for child in body:
        if child.tag==tag+'p':collector.add(paragraph(child))
        elif child.tag==tag+'tbl':
            tables+=1;rows=[]
            for number,row in enumerate(child.findall('w:tr',ns),1):
                values=[]
                for cell in row.findall('w:tc',ns):
                    text='\n'.join(paragraph(p) for p in cell.findall('.//w:p',ns))
                    span=cell.find('w:tcPr/w:gridSpan',ns);merge=cell.find('w:tcPr/w:vMerge',ns)
                    if span is not None:text+=' [跨'+span.get(tag+'val','1')+'列]'
                    if merge is not None:text+=' [纵向合并单元格]'
                    values.append(text)
                rows.append((number,values))
            table_rows(collector,'Word表格'+str(tables),rows)
    return {'tables':tables}

def excel_text(content,suffix,collector,warnings):
    if suffix=='.xlsx':
        from openpyxl import load_workbook
        with checked_archive(content) as z:
            # Macro-enabled archives must not be disguised as normal workbooks.
            if any(name.lower().endswith('vbaproject.bin') for name in z.namelist()):raise PipelineError('请将宏工作簿另存为不含宏的XLSX。')
            merged={name:[n.attrib['ref'] for n in ET.fromstring(z.read(name)).iter() if n.tag.endswith('}mergeCell')] for name in z.namelist() if name.startswith('xl/worksheets/sheet') and name.endswith('.xml')}
        book=load_workbook(io.BytesIO(content),read_only=True,data_only=False,keep_links=False)
        cached=None
        try:
            if len(book.worksheets)>50:raise PipelineError('工作表超过50张，请拆分。')
            cached=load_workbook(io.BytesIO(content),read_only=True,data_only=True,keep_links=False)
            formulas=0;missing=0
            for sheet,cache in zip(book.worksheets,cached.worksheets):
                if (sheet.max_row or 0)>10000 or (sheet.max_column or 0)>256 or (sheet.max_row or 0)*(sheet.max_column or 0)>100000:raise PipelineError('工作表范围超过1万行、256列或10万单元格，请清理多余格式或拆分。')
                title='工作表：'+sheet.title+(' [隐藏]' if sheet.sheet_state!='visible' else '')
                collector.add(title)
                ranges=merged.get(sheet._worksheet_path.lstrip('/'),[])
                if ranges:collector.add(title+' · 合并区域：'+', '.join(ranges))
                def rows():
                    nonlocal formulas,missing
                    for number,(row,cache_row) in enumerate(zip(sheet.iter_rows(),cache.iter_rows()),1):
                        values=[]
                        for cell,cached_cell in zip(row,cache_row):
                            value=cell.value
                            if cell.data_type=='f':
                                formulas+=1;cached_value=cached_cell.value
                                if cached_value is None:missing+=1
                                value=f'[公式：{value}；'+('无缓存结果，未计算]' if cached_value is None else '缓存值：'+cell_value(cached_value)+'，未重新计算]')
                            elif isinstance(value,(float,int)) and not isinstance(value,bool) and cell.number_format!='General':value=cell_value(value)+' [单元格格式：'+cell.number_format+']'
                            values.append(value)
                        yield number,values
                table_rows(collector,title,rows())
            if formulas:warnings.append(f'含{formulas}个公式，仅保留公式与文件内缓存值，不执行计算；其中{missing}个没有缓存结果。')
            return {'sheets':len(book.worksheets),'formulas':formulas,'missing_formula_results':missing}
        finally:
            book.close()
            if cached:cached.close()
    import xlrd
    book=xlrd.open_workbook(file_contents=content,on_demand=True)
    try:
        if book.nsheets>50:raise PipelineError('工作表超过50张，请拆分。')
        for sheet in book.sheets():
            if sheet.nrows>10000 or sheet.ncols>256 or sheet.nrows*sheet.ncols>100000:raise PipelineError('工作表范围过大，请拆分。')
            title='工作表：'+sheet.name;collector.add(title)
            def rows():
                for row in range(sheet.nrows):
                    values=[]
                    for col in range(sheet.ncols):
                        cell=sheet.cell(row,col);v=cell.value
                        if cell.ctype==xlrd.XL_CELL_DATE:v=xlrd.xldate_as_datetime(v,book.datemode).isoformat()
                        elif cell.ctype==xlrd.XL_CELL_ERROR:v='[单元格错误：'+xlrd.error_text_from_code.get(v,str(v))+']'
                        values.append(v)
                    yield row+1,values
            table_rows(collector,title,rows())
        warnings.append('XLS仅读取单元格保存值；不执行宏或公式，不提取图表与嵌入对象。')
        return {'sheets':book.nsheets}
    finally:book.release_resources()

def extract_document(content,suffix):
    suffix=suffix.lower();warnings=[];metadata={};collector=Collector()
    if len(content)>MAX_BYTES:raise PipelineError('文件不能超过10MB。')
    if suffix=='.doc':raise PipelineError('旧版Word DOC暂不支持，请先另存为DOCX或文字PDF后上传。')
    if suffix not in SUPPORTED:raise PipelineError('支持PDF、Word DOCX、Excel XLSX/XLS、CSV、TSV、TXT和Markdown。')
    try:
        if suffix in ('.txt','.md'):
            try:collector.add(content.decode('utf-8-sig'))
            except UnicodeDecodeError:raise PipelineError('TXT/Markdown请使用UTF-8编码。')
        elif suffix in ('.csv','.tsv'):
            try:text=content.decode('utf-8-sig')
            except UnicodeDecodeError:
                try:text=content.decode('gb18030');warnings.append('文件已按GB18030中文编码读取，请检查字符。')
                except UnicodeDecodeError:raise PipelineError('表格编码无法识别，请另存为UTF-8 CSV。')
            table_rows(collector,'数据表',enumerate(csv.reader(io.StringIO(text),delimiter='\t' if suffix=='.tsv' else ','),1))
        elif suffix=='.docx':
            metadata=docx_text(content,collector);warnings.append('提取Word正文与表格；图片、图表、页眉页脚及部分自动编号需人工核对。')
        elif suffix in ('.xlsx','.xls'):
            metadata=excel_text(content,suffix,collector,warnings);warnings.append('按工作表和单元格位置提取；隐藏数据、合并区域及格式需核对，不读取图片和图表。')
        elif suffix=='.pdf':
            if not content.startswith(b'%PDF-'):raise PipelineError('文件内容不是PDF。')
            from pypdf import PdfReader
            pdf=PdfReader(io.BytesIO(content))
            if pdf.is_encrypted or len(pdf.pages)>250:raise PipelineError('暂不支持加密PDF或超过250页的PDF。')
            empty=[]
            for number,page in enumerate(pdf.pages,1):
                text=page.extract_text() or ''
                if not text.strip():empty.append(number)
                else:collector.add(f'PDF第{number}页\n'+text)
            if empty:warnings.append('以下页未提取到文字，可能为空白或扫描页：'+','.join(map(str,empty))+'。需人工核对/OCR。')
            metadata={'pages':len(pdf.pages),'pages_without_text':empty}
    except PipelineError:raise
    except ImportError:raise PipelineError('解析依赖尚未安装，请按requirements.txt更新环境。')
    except Exception:raise PipelineError('文件无法解析，可能已损坏、加密或内容与扩展名不符。请另存为支持格式后重试。')
    text=collector.text().replace('\r\n','\n').replace('\r','\n').strip()
    if suffix in ('.xlsx','.xls','.csv','.tsv') and collector.data_characters<12:raise PipelineError('表格有效内容不足，不能仅凭工作表名称或空表入库。')
    if len(text)<12:raise PipelineError('提取文字不足，扫描件需先OCR，不能直接启用。')
    if '\x00' in text:raise PipelineError('文档含无效字符。')
    return {'text':text,'format':suffix.lstrip('.').upper(),'warnings':warnings,'extraction':metadata}

def extract(content,suffix):return extract_document(content,suffix)['text']
