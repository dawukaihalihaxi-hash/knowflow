document.querySelectorAll('time[datetime]').forEach(t=>{const d=new Date(t.dateTime);t.title=t.dateTime;t.textContent=d.toLocaleString('zh-CN',{hour12:false});});
