/* ---------- F9 — generated docs (cutover playbook + as-built) ---------- */
async function loadDocs(){
  const tb = $('docTbl').querySelector('tbody'); tb.innerHTML='';
  try{
    const arts = await api('/docs');
    arts.forEach(d=>{
      const col = d.doc_type==='cutover' ? '#3b8eea' : d.doc_type==='as_built' ? 'var(--green)' : 'var(--fg)';
      tb.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="doc_type"><span style="color:${col};font-weight:600">${esc(d.doc_type||'-')}</span></td>
        <td data-label="scope_id">${esc(d.scope_id||'-')}</td>
        <td data-label="scanned">${esc(d.scanned_at||'-')}</td>
        <td data-label="summary" class="conf" title="${esc(d.summary||'')}">${esc((d.summary||'-').slice(0,60))}</td>
        <td data-label="view"><button class="sm" onclick="showDoc('${attr(d.doc_type)}','${attr(d.scope_id)}')">view</button></td></tr>`);
    });
    if(!arts.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="5" class="muted">no docs yet — pick a wave and generate a cutover playbook or as-built.</td></tr>`);
  }catch(e){ tb.innerHTML = `<tr><td colspan="5" class="ev-err">${esc(e)}</td></tr>`; }
}

async function showDoc(docType, scopeId){
  const el = $('docDetail'); el.classList.remove('hidden'); el.innerHTML='<span class="spinner"></span>';
  const slug = docType === 'as_built' ? 'as-built' : docType;
  try{
    const d = await api('/docs/'+encodeURIComponent(slug)+'/'+encodeURIComponent(scopeId));
    el.innerHTML = `<div class="row" style="justify-content:space-between;align-items:center">
        <div><b>${esc(d.doc_type)}</b> · ${esc(d.scope_id)}</div>
        <button class="sm" onclick="downloadDoc('${attr(d.doc_type)}','${attr(d.scope_id)}')">download .md</button>
      </div>
      <pre class="mcard" style="margin-top:6px;white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(d.doc_md||'(empty)')}</pre>`;
  }catch(e){ el.innerHTML='<span class="ev-err">'+esc(e)+'</span>'; }
}

function downloadDoc(docType, scopeId){
  const slug = docType === 'as_built' ? 'as-built' : docType;
  fetch(API+'/docs/'+encodeURIComponent(slug)+'/'+encodeURIComponent(scopeId))
    .then(r=>r.json()).then(d=>{
      const blob = new Blob([d.doc_md||''], {type:'text/markdown'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${docType}-${scopeId}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
    }).catch(e=> toast('download failed: '+e,'err'));
}

async function doDocGen(kind){
  // kind = "cutover" | "as-built"
  const wave_id = $('docWave').value.trim();
  if(!wave_id){ toast('enter a wave_id (from the Waves tab)','warn'); return; }
  const endpoint = kind === 'as-built' ? '/as-built' : '/cutover-playbook';
  try{
    await api(endpoint, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({wave_id, context:{}})});
    toast(`${kind} generation requested — doc arrives via callback.`, 'ok');
    setTimeout(loadDocs, 1500);
  }catch(e){ toast(`${kind} gen failed: `+e, 'err'); }
}