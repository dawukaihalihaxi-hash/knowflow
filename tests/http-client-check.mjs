import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
const script=await fs.readFile(new URL('../static/http.js',import.meta.url),'utf8');
function harness(responses){
 const csrf={content:'old-token'},version={content:'v2'},calls=[];
 const context={window:{},document:{querySelector:s=>s.includes('csrf-token')?csrf:version},fetch:async(url,init)=>{calls.push({url,init});const item=responses.shift();assert.ok(item,'unexpected fetch');return {status:item.status,ok:item.status>=200&&item.status<300,headers:{get:key=>key==='content-type'?(item.type||'application/json'):key==='X-KnowFlow-Version'?(item.version||'v2'):null},json:async()=>{if(item.invalid)throw new SyntaxError('malformed');return item.data;}};}};
 vm.runInNewContext(script,context);return {call:context.window.appRequest,calls,csrf};
}
await assert.rejects(harness([{status:404,type:'text/html'}]).call('/api/missing'),/非JSON响应.*404/);
await assert.rejects(harness([{status:500,invalid:true}]).call('/api/missing'),/响应损坏/);
await assert.rejects(harness([{status:405,data:{error:'请求方法不支持。'}}]).call('/api/wrong-method'),/请求方法不支持/);
await assert.rejects(harness([{status:200,version:'v1',data:{ok:true}}]).call('/api/version'),/版本不一致/);
const h=harness([{status:403,data:{code:'csrf_expired'}},{status:200,data:{csrf:'new-token'}},{status:200,data:{ok:true}}]);
assert.equal((await h.call('/api/delete',{method:'POST'})).ok,true);assert.equal(h.calls.length,3);assert.equal(h.calls[2].init.headers['X-CSRF-Token'],'new-token');
console.log('HTTP client: 5 response/error contracts passed');
