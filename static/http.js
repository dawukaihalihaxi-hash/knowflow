window.appRequest=async function(url,options={}){
 const meta=document.querySelector('meta[name="csrf-token"]');
 const version=document.querySelector('meta[name="app-version"]')?.content;
 let token=meta?.content||'';
 async function send(target=url,init=options){
  const response=await fetch(target,{...init,cache:'no-store',headers:{...init.headers,'X-CSRF-Token':token,...(version?{'X-KnowFlow-Version':version}:{})}});
  const type=response.headers.get('content-type')||'';
  if(!type.includes('application/json'))throw Error(`服务返回非JSON响应（HTTP ${response.status}）。请刷新页面；若仍失败，请核对服务端口和版本。`);
  let data;try{data=await response.json();}catch{throw Error(`接口响应损坏（HTTP ${response.status}），请刷新后重试。`);}
  const server=response.headers.get('X-KnowFlow-Version');
  if(version&&server&&version!==server)throw Error('页面与接口版本不一致，请刷新页面后重试。');
  return {response,data};
 }
 let {response,data}=await send();
 if(response.status===403&&data.code==='csrf_expired'){
  const fresh=await send('/api/session',{});
  if(!fresh.response.ok)throw Error(fresh.data.error||'请重新登录。');
  token=fresh.data.csrf;if(meta)meta.content=token;
  ({response,data}=await send());
 }
 if(!response.ok)throw Error(data.error||`请求失败（HTTP ${response.status}），请稍后重试。`);
 return data;
};
