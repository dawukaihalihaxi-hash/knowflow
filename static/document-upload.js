// Shared multi-file queue for the setup wizard and assistant management.
window.DocumentUploads=(()=>{
 const caps=appRequest('/api/admin/document-capabilities');
 const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 function create({input,container,summary}){
  let items=[],busy=false,limits={extensions:['.pdf','.docx','.xlsx','.xls','.csv','.tsv','.txt','.md'],max_bytes:10*1024*1024,max_batch_files:20};
  const labels={pending:'待上传',uploading:'正在解析…',uploaded:'已上传，待构建',existing:'已存在，未重复导入',failed:'失败'};
  const totals=()=>({total:items.length,uploaded:items.filter(x=>x.status==='uploaded').length,existing:items.filter(x=>x.status==='existing').length,failed:items.filter(x=>x.status==='failed').length,pending:items.filter(x=>x.status==='pending').length});
  function render(){
   container.replaceChildren();
   for(const item of items){const row=node('div',undefined,'upload-queue-row');const info=node('div');info.append(node('strong',item.file.name),node('small',(item.file.size/1024).toFixed(1)+' KB · '+labels[item.status]));if(item.error)info.append(node('p',item.error,'upload-file-error'));const remove=node('button','移出列表','text-button');remove.type='button';remove.disabled=busy;remove.title='只从本次上传列表移出，不删除已经保存的资料';remove.onclick=()=>{items=items.filter(x=>x!==item);render();};row.append(info,remove);container.append(row);}
   if(items.some(x=>['uploaded','existing'].includes(x.status))){const clear=node('button','清空已完成项（不删除已保存资料）','text-button');clear.type='button';clear.disabled=busy;clear.onclick=()=>{items=items.filter(x=>!['uploaded','existing'].includes(x.status));render();};container.append(clear);}
   const t=totals();summary.textContent=t.total?`共${t.total}份：待上传${t.pending}，已提取${t.uploaded}，已存在${t.existing}，失败${t.failed}。`:'可一次选择多份资料，也可分批追加。';
  }
  input.addEventListener('change',()=>{
   let skipped=0;
   for(const file of Array.from(input.files)){
    if(items.some(x=>x.file.name===file.name&&x.file.size===file.size&&x.file.lastModified===file.lastModified))continue;
    if(items.length>=limits.max_batch_files){skipped++;continue;}
    const suffix='.'+file.name.split('.').at(-1).toLowerCase();let error='';
    if(!limits.extensions.includes(suffix))error=suffix==='.doc'?'旧版DOC请先另存为DOCX或文字PDF。':'此格式暂不支持，请转换为下方列出的格式。';
    else if(file.size>limits.max_bytes)error='文件超过10MB，请拆分后上传。';
    items.push({file,status:error?'failed':'pending',error,retryable:!error});
   }
   input.value='';render();if(skipped)summary.textContent+=' 本批最多'+limits.max_batch_files+'份，另有'+skipped+'份未加入；请分批上传。';
  });
  caps.then(c=>{limits=c;input.accept=c.extensions.join(',');render();}).catch(()=>{render();});
  render();
  return {
   count:()=>items.length,
   results:()=>items.map(x=>({name:x.file.name,status:x.status,error:x.error,document:x.document})),
   async upload(endpoint){
    if(busy)throw Error('本批资料正在处理，请稍候。');busy=true;input.disabled=true;
    try{for(const item of items){
     if(item.status!=='pending'&&!(item.status==='failed'&&item.retryable))continue;
     item.status='uploading';item.error='';render();
     try{const fd=new FormData();fd.append('file',item.file);const d=await appRequest(endpoint,{method:'POST',body:fd});item.document=d;item.status=d.duplicate?'existing':'uploaded';}
     catch(error){item.status='failed';item.error=error.message;item.retryable=true;}
     render();
    }}finally{busy=false;input.disabled=false;render();}
    return totals();
   }
  };
 }
 return {create};
})();
