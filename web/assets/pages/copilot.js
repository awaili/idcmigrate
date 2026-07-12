/* ---------- copilot ---------- */
async function doAsk(){ const q=$('askQ').value.trim(); if(!q)return; $('askOut').innerHTML='<span class="spinner"></span>'; try{const r=await api('/ask',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({question:q})}); $('askOut').innerHTML=`<pre>${esc(r.answer)}</pre>`;}catch(e){ $('askOut').innerHTML='<span class="ev-err">Error: '+e+'</span>'; } }
function stopAgent(){ if(ws){try{ws.close()}catch(e){}} }
async function doAgent(){ const prompt=$('agPrompt').value.trim(); if(!prompt)return; const mode=$('agMode').value; const out=$('agOut'); out.innerHTML=''; $('agStatus').textContent='starting…';
  let tid; try{ const r=await api('/agent',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({prompt,mode,timeout:600})}); tid=r.task_id; }catch(e){ out.innerHTML='<span class="ev-err">Error: '+e+'</span>'; return; }
  ws=new WebSocket(`${location.origin.replace('http','ws')}/ws/agent/${tid}`); const buf=[];
  ws.onmessage=m=>{const ev=JSON.parse(m.data); if(ev.kind==='text'){buf.push(ev.text);out.innerHTML=`<pre>${esc(buf.join(''))}</pre>`;out.scrollTop=out.scrollHeight;} else if(ev.kind==='tool')out.insertAdjacentHTML('beforeend',`<div class="ev-tool">${esc(ev.text)}</div>`); else if(ev.kind==='result'){out.insertAdjacentHTML('beforeend',`<div class="muted">[done]</div>`);$('agStatus').textContent='done';} else if(ev.kind==='error'){out.insertAdjacentHTML('beforeend',`<div class="ev-err">${esc(ev.text)}</div>`);$('agStatus').textContent='error';}};
  ws.onclose=()=>{if(!['done','error'].includes($('agStatus').textContent))$('agStatus').textContent='closed';};
}

/* ---------- What-if (F3 — re-price the portfolio without re-ingest) ---------- */
async function runWhatIf(){
  const out=$('wiOut');
  const body={};
  const region=$('wiRegion').value; if(region) body.region=region;
  const sizing=$('wiSizing').value; if(sizing) body.sizing=sizing;
  if(!region && !sizing){ toast('pick a region and/or sizing first','warn'); return; }
  out.innerHTML='<span class="spinner"></span> running what-if…';
  try{
    const r=await api('/cost/what-if',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
    if(r.alternate==null){ out.innerHTML='<span class="muted">no override applied — pick a region and/or sizing.</span>'; return; }
    const dy=r.delta; const sign=dy<0?'save':(dy>0?'cost +':'no change');
    const fmt=n=>`$${Number(n).toLocaleString()}`;
    out.innerHTML=
      `<div class="row" style="gap:18px;flex-wrap:wrap">
        <div class="tile"><div class="tlabel">baseline yearly</div><div class="tval">${fmt(r.baseline.cloud_yearly)}</div><div class="tsub muted">${r.baseline.priced_servers} priced</div></div>
        <div class="tile"><div class="tlabel">alternate yearly</div><div class="tval">${fmt(r.alternate.cloud_yearly)}</div><div class="tsub muted">${r.alternate.priced_servers} priced</div></div>
        <div class="tile"><div class="tlabel">delta</div><div class="tval ${dy<0?'ev-ok':(dy>0?'ev-err':'')}">${fmt(dy)}</div><div class="tsub muted">${sign}/yr</div></div>
       </div>
       <div class="muted" style="margin-top:6px">applied: region=${esc(r.applied.region||'-')}, sizing=${esc(r.applied.sizing||'-')}, byol=${esc(r.applied.byol==null?'-':r.applied.byol)}</div>`;
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
}
