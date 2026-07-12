/* ---------- code intelligence (external executor) ---------- */
async function loadCode(){
  loadExecStatus();   // connected/disconnected badge
  try{
    const profiles = await api('/code-profiles');
    const tb = $('codeTbl').querySelector('tbody'); tb.innerHTML='';
    // datalist of known app_ids (from profiles) so the 7R input can autocomplete
    const dl = $('srAppList'); if(dl){ dl.innerHTML = profiles.map(p=>`<option value="${esc(p.app_id)}">`).join(''); }
    profiles.forEach(p=>{
      tb.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="app_id">${esc(p.app_id)}</td><td data-label="pattern">${esc(p.migration_pattern||'-')}</td>
        <td data-label="effort">${esc(p.refactor_effort||'-')}</td>
        <td data-label="readiness">${p.cloud_readiness==null?'-':p.cloud_readiness.toFixed(2)}</td>
        <td data-label="code_deps">${esc((p.code_deps||[]).join(', ')||'-')}</td>
        <td data-label="blockers">${(p.blockers||[]).length}</td><td data-label="findings">${(p.findings||[]).length}</td>
        <td data-label="scanned">${esc(p.scanned_at||'-')}</td>
        <td data-label="7R"><button class="sm" onclick="assignStrategy('${esc(p.app_id)}')">7R</button></td></tr>`);
    });
    if(!profiles.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="9" class="muted">no profiles yet — trigger a scan, or wait for executor callback.</td></tr>`);
  }catch(e){ $('codeTbl').querySelector('tbody').innerHTML = `<tr><td colspan="9" class="ev-err">${esc(e)}</td></tr>`; }
  try{
    const dbs = await api('/db-profiles');
    const dbt = $('dbTbl').querySelector('tbody'); dbt.innerHTML='';
    dbs.forEach(d=>{
      const rev = d.reverse_replication ? '<span style="color:var(--green)">✓</span>' : '<span class="muted">✗</span>';
      const auto = (d.auto_convert_pct!=null) ? (d.auto_convert_pct*100).toFixed(0)+'%' : '-';
      dbt.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="hostname">${esc(d.db_server_id||'-')}</td>
        <td data-label="conversion">${esc(d.source_engine||'-')} → ${esc(d.target_engine||'-')}</td>
        <td data-label="grade"><span style="color:${_gradeColor(d.difficulty)};font-weight:600">${esc(d.difficulty||'-')}</span></td>
        <td data-label="man-days">${d.est_man_days!=null?esc(d.est_man_days):'-'}</td>
        <td data-label="auto %">${auto}</td>
        <td data-label="review">${(d.review_objects||[]).length}</td>
        <td data-label="blockers">${(d.blockers||[]).length}</td>
        <td data-label="rev-repl">${rev}</td>
        <td data-label="scanned">${esc(d.scanned_at||'-')}</td></tr>`);
    });
    if(!dbs.length) dbt.insertAdjacentHTML('beforeend', `<tr><td colspan="9" class="muted">no DB profiles yet — trigger a DB scan, or wait for the executor to push one.</td></tr>`);
  }catch(e){ $('dbTbl').querySelector('tbody').innerHTML = `<tr><td colspan="9" class="ev-err">${esc(e)}</td></tr>`; }
  try{
    const jobs = await api('/change-jobs');
    const jb = $('jobTbl').querySelector('tbody'); jb.innerHTML='';
    jobs.forEach(j=> jb.insertAdjacentHTML('beforeend', `<tr>
      <td data-label="id">${esc(j.id)}</td><td data-label="app_id">${esc(j.app_id)}</td><td data-label="kind">${esc(j.kind)}</td>
      <td data-label="status">${esc(j.status)}</td><td data-label="summary" class="conf" title="${esc(j.summary||'')}">${esc((j.summary||'-').slice(0,60))}</td>
      <td data-label="patch_ref">${esc(j.patch_ref||'-')}</td><td data-label="created">${esc(j.created_at||'-')}</td></tr>`));
    if(!jobs.length) jb.insertAdjacentHTML('beforeend', `<tr><td colspan="7" class="muted">no executor jobs yet.</td></tr>`);
  }catch(e){ $('jobTbl').querySelector('tbody').innerHTML = `<tr><td colspan="7" class="ev-err">${esc(e)}</td></tr>`; }
  loadQuestions();   // refresh the pending-questions queue alongside jobs
  startQuestionPolling();
}
async function doExecutorScan(){
  const action = $('exAction').value;
  const body = {app_id:$('exApp').value.trim(), repo_url:$('exRepo').value.trim(),
                branch:$('exBranch').value.trim(), action, mode:$('exMode').value};
  if(!body.app_id || !body.repo_url){ $('exOut').innerHTML='<span class="ev-err">app_id and repo url required</span>'; return; }
  if(action === 'modify'){
    const s = $('exScope').value.trim();
    if(s) body.scope = s.split(',').map(x=>x.trim()).filter(Boolean);
    const ov = parseOverrides($('exOverrides').value);
    if(Object.keys(ov).length) body.overrides = ov;
  }
  $('exOut').innerHTML='<span class="spinner"></span>';
  try{
    const r = await fetch(API+'/executor/trigger',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
    const txt = await r.text();
    if(!r.ok) throw new Error(txt);
    const j = JSON.parse(txt);
    const chg = (j.changes&&j.changes.length)
      ? `<div class="muted">${j.changes.length} concrete change(s):</div><pre>${esc(j.changes.map(c=>`· [${c.category}] ${c.file}:${c.line||'?'}  ${c.old||'(?)'} → ${c.new||'(?)'}`).join('\n'))}</pre>`
      : '';
    const notes = (j.notes&&j.notes.length) ? `<div class="muted">notes: ${esc(j.notes.join('; '))}</div>` : '';
    $('exOut').innerHTML = `<pre>${esc(txt)}</pre>${chg}${notes}<div class="muted">Executor runs async and calls back into /api/code-profiles + /api/change-jobs. Hit Rebuild to fold results into waves.</div>`;
    setTimeout(loadCode, 1500);
  }catch(e){ $('exOut').innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; }
}

/* show the scope/overrides fields only for modify */
function toggleModifyFields(){
  $('exModifyFields').classList.toggle('hidden', $('exAction').value !== 'modify');
}

/* ---------- 7R strategy (AI assigns 6R + rehost-container per app) ---------- */
const _7R_COLORS = {
  'rehost':'var(--green)','rehost-container':'var(--green)',
  'replatform':'#3b8eea','refactor':'var(--amber)','repurchase':'#a06bff',
  'retain':'#888','retire':'#e5484d'
};
function _strategyBadge(s){
  if(!s) return '<span class="muted">—</span>';
  const c = _7R_COLORS[s] || 'var(--fg)';
  return `<span style="color:${c};font-weight:600">${esc(s)}</span>`;
}
async function assignStrategy(appId){
  const app_id = appId || ($('srApp') && $('srApp').value.trim());
  const out = $('srOut');
  if(!app_id){ out.innerHTML = '<span class="ev-err">enter an app_id first</span>'; return; }
  const apply = $('srApply') && $('srApply').checked;
  out.innerHTML = `<span class="spinner"></span> asking the LLM for a 7R strategy for ${esc(app_id)}…`;
  let r; try{
    r = await api('/strategy', {method:'POST', headers:{'content-type':'application/json'},
              body:JSON.stringify({app_id, apply})});
  }catch(e){ out.innerHTML = '<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  out.innerHTML = _renderStrategy(r, apply);
  if(apply) loadCode();   // refresh the profiles table so the new pattern shows
}
let _batchAbort = null;
async function assignStrategyAll(){
  const out = $('srOut');
  const apply = $('srApply') && $('srApply').checked;
  // streamed from the backend (NDJSON): one result per app, live progress, cancellable.
  // Replaces the old browser-side N-call loop that hung for large N.
  if(_batchAbort){ _batchAbort.abort(); return; }   // a running batch -> cancel it
  _batchAbort = new AbortController();
  out.innerHTML = '<span class="spinner"></span> starting batch 7R… <button class="sm" onclick="assignStrategyAll()">cancel</button>';
  const rows = []; let total = 0, done = 0, okCount = 0;
  let resp;
  try{
    resp = await fetch(API+'/strategy/batch', {
      method:'POST', headers:{'content-type':'application/json'},
      body:JSON.stringify({apply}), signal:_batchAbort.signal
    });
  }catch(e){ _batchAbort=null; out.innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf='';
  const render = () => {
    out.innerHTML = `<div class="muted">batch 7R: ${done}/${total||'?'} done, ${okCount} assigned${apply?' (applied)':''} <button class="sm" onclick="assignStrategyAll()">cancel</button></div>`
      + rows.slice(-200).map(r=>`<div class="muted">· ${esc(r.app_id)}: ${r.ok?_strategyBadge(r.strategy):'<span class="ev-err">'+esc(r.error||'failed')+'</span>'}${r.applied?' <span style="color:var(--green)">✓</span>':''}</div>`).join('');
  };
  try{
    while(true){
      const {value, done:rd} = await reader.read();
      if(rd) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for(const line of lines){
        if(!line) continue;
        let j; try{ j = JSON.parse(line); }catch(e){ continue; }
        if(j.type==='start'){ total = j.total; }
        else if(j.type==='result'){ rows.push(j); done = j.done; if(j.ok) okCount++; render(); }
        else if(j.type==='done'){ done = j.total; okCount = j.ok_count; }
      }
    }
  }catch(e){ /* abort / network */ }
  _batchAbort = null;
  out.innerHTML = `<div class="muted">batch 7R done: ${okCount}/${done} assigned${apply?' (applied)':''}.</div>`
    + rows.map(r=>`<div class="muted">· ${esc(r.app_id)}: ${r.ok?_strategyBadge(r.strategy):'<span class="ev-err">'+esc(r.error||'failed')+'</span>'}</div>`).join('');
  loadCode();
}
function _renderStrategy(r, apply){
  if(!r || !r.ok){
    return `<span class="ev-err">7R assignment failed: ${esc((r&&r.error)||'unknown')}</span>`
      + (r&&r.raw ? `<details class="mcard" style="margin-top:4px;padding:4px 8px"><summary class="muted">raw LLM output</summary><pre>${esc((r.raw||'').slice(0,800))}</pre></details>` : '');
  }
  const kc = (r.key_changes||[]).map(k=>`<li>${esc(k)}</li>`).join('');
  const applied = r.applied===true ? `<span style="color:var(--green)">✓ written to app_strategies</span>`
               : r.applied===false ? `<span style="color:var(--amber)">⚠ ${esc(r.apply_note||'not applied')}</span>` : '';
  const note = (r.error && r.ok) ? `<div class="muted">${esc(r.error)}</div>` : '';  // e.g. engine force-merge note (not used here)
  return `
    <div class="row" style="gap:14px;align-items:baseline;flex-wrap:wrap">
      <div><span class="muted">app</span> <b>${esc(r.app_id)}</b></div>
      <div><span class="muted">strategy</span> ${_strategyBadge(r.strategy)}</div>
      <div><span class="muted">target</span> <b>${esc(r.target||'-')}</b></div>
      <div><span class="muted">effort</span> ${esc(r.effort||'-')}</div>
      <div><span class="muted">confidence</span> ${(r.confidence!=null?r.confidence.toFixed(2):'-')}</div>
    </div>
    <div style="margin:6px 0">${esc(r.rationale||'')}</div>
    ${kc?`<div class="muted">key changes:</div><ul style="margin:2px 0 6px 18px">${kc}</ul>`:''}
    ${applied?`<div>${applied}</div>`:''}
    ${note}`;
}

/* parse a textarea of "old=new" lines into a {old:new} map */
function parseOverrides(text){
  const out = {};
  for(const line of (text||'').split('\n')){
    const i = line.indexOf('=');
    if(i > 0){ const k = line.slice(0,i).trim(), v = line.slice(i+1).trim(); if(k) out[k] = v; }
  }
  return out;
}
async function doUpload(file){
  if(!file) return;
  const source=$('uploadSrc').value;
  const fd=new FormData(); fd.append('source',source); fd.append('file',file);
  try{
    const r=await fetch(API+'/ingest/upload',{method:'POST',body:fd});
    if(!r.ok) throw new Error(await r.text());
    const j=await r.json();
    toast(`Uploaded ${source}: ${j.run&&j.run.raw_count} raw assets (mode=${j.run&&j.run.mode}). Run Rebuild to merge.`,'ok');
  }catch(e){ toast('Upload failed: '+e,'err'); }
  $('uploadFile').value='';
}

/* executor connectivity badge — powers the "is the code agent connected" indicator */
async function loadExecStatus(){
  const el = $('execStatus'); if(!el) return;
  el.innerHTML = '<span class="spinner"></span>';
  try{
    const s = await api('/executor/status');
    const color = s.reachable ? 'var(--green)' : (s.configured ? 'var(--amber)' : 'var(--fg)');
    const label = s.reachable ? `● connected${s.version?` · ${esc(s.version)}`:''}`
               : s.configured ? `● configured, unreachable`
               : '○ not configured (IDC_EXECUTOR_URL)';
    const tok = s.token_set ? '' : ' · token unset';
    const dis = (s.enabled===false) ? ' · disabled' : '';
    el.innerHTML = `<span style="color:${color}">${label}${tok}${dis}</span>`;
    el.title = s.detail || '';
  }catch(e){ el.innerHTML = `<span style="color:var(--amber)">● status unavailable</span>`; }
}

/* ---------- F5 — DB conversion profiles ---------- */
function _gradeColor(g){ return g==='A'?'var(--green)':g==='B'?'var(--amber)':g==='C'?'var(--red)':'var(--fg)'; }
async function doDbScan(){
  const db_server_id = $('dbScanId').value.trim();
  if(!db_server_id){ toast('enter a db hostname first','warn'); return; }
  const body = { db_server_id, source_engine:$('dbSrcEng').value, target_engine:$('dbTgtEng').value };
  try{
    await api('/db-scan', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast('DB scan requested — profile arrives via callback; Rebuild folds the grade in.', 'ok');
    setTimeout(loadCode, 1500);
  }catch(e){ toast('DB scan failed: '+e, 'err'); }
}

/* ---------- F9 — runtime containerization (no-source path) ---------- */
async function prefillRuntimeInventory(){
  const sid = $('rtServer').value.trim();
  if(!sid){ toast('enter server_id first','warn'); return; }
  try{
    const inv = await api(`/runtime-inventory/${encodeURIComponent(sid)}`);
    $('rtProc').value = inv.process || '';
    $('rtPort').value = (inv.ports||[]).join(',');
    $('rtSoft').value = (inv.software||[]).join(',');
    const b = inv.basis || {};
    toast(`pre-filled from server (ports: ${b.ports||'none'}, software: ${b.software||'none'}, process: ${b.process||'none'})`, 'ok');
  }catch(e){ toast('pre-fill failed: '+e, 'err'); }
}
async function doRuntimeContainerize(){
  const app_id = $('rtApp').value.trim();
  const server_id = $('rtServer').value.trim();
  if(!app_id || !server_id){ toast('enter app_id + server_id','warn'); return; }
  // send only what the operator typed; the backend auto-gathers the gaps + merges
  const inv = {};
  if($('rtProc').value.trim()) inv.process = $('rtProc').value.trim();
  if($('rtPort').value.trim()) inv.ports = $('rtPort').value.split(',').map(s=>parseInt(s.trim())).filter(n=>n);
  if($('rtSoft').value.trim()) inv.software = $('rtSoft').value.split(',').map(s=>s.trim()).filter(s=>s);
  const body = { app_id, server_id, inventory: inv, mode:$('rtMode').value };
  try{
    await api('/runtime-containerize', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast('Runtime containerize requested — profile arrives via callback (source=runtime-derived).', 'ok');
    setTimeout(loadCode, 1500);
  }catch(e){ toast('runtime-containerize failed: '+e, 'err'); }
}
