/* ---------- code intelligence (external executor) ---------- */
async function loadCode(){
  loadExecStatus();   // default-executor connected/disconnected badge (header)
  loadExecutors();    // registry list (default + named) — manage panel
  populateExecPickers();  // fill the per-trigger executor <select>s
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
        <td data-label="7R"><button class="sm" onclick="assignStrategy('${attr(p.app_id)}')">7R</button></td></tr>`);
    });
    if(!profiles.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="9" class="muted">no profiles yet — trigger a scan, or wait for executor callback.</td></tr>`);
  }catch(e){ $('codeTbl').querySelector('tbody').innerHTML = `<tr><td colspan="9" class="ev-err">${esc(e)}</td></tr>`; }
  try{
    const dbs = await api('/db-profiles');
    const dbt = $('dbTbl').querySelector('tbody'); dbt.innerHTML='';
    dbs.forEach(d=>{
      const rev = d.reverse_replication ? '<span style="color:var(--green)">✓</span>' : '<span class="muted">✗</span>';
      const auto = (d.auto_convert_pct!=null) ? (d.auto_convert_pct*100).toFixed(0)+'%' : '-';
      const hasConv = d.conversion && (d.conversion.objects||[]).length;
      const blocked = hasConv ? (d.conversion.objects||[]).filter(o=>o.status==='blocked').length : 0;
      const convTag = hasConv
        ? `<span style="color:${blocked?'var(--red)':'var(--green)'}" title="${blocked} blocked object(s)">${blocked?'⚠ '+blocked+' blocked':'✓ '+hasConv+' obj'}</span>`
        : '<span class="muted">—</span>';
      const convBtn = hasConv ? `<button class="sm" onclick="showDbConv('${attr(d.db_server_id)}')">report</button>` : '';
      dbt.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="hostname">${esc(d.db_server_id||'-')}</td>
        <td data-label="conversion">${esc(d.source_engine||'-')} → ${esc(d.target_engine||'-')}</td>
        <td data-label="grade"><span style="color:${_gradeColor(d.difficulty)};font-weight:600">${esc(d.difficulty||'-')}</span></td>
        <td data-label="man-days">${d.est_man_days!=null?esc(d.est_man_days):'-'}</td>
        <td data-label="auto %">${auto}</td>
        <td data-label="review">${(d.review_objects||[]).length}</td>
        <td data-label="blockers">${(d.blockers||[]).length}</td>
        <td data-label="rev-repl">${rev}</td>
        <td data-label="conv?">${convTag} ${convBtn}</td>
        <td data-label="scanned">${esc(d.scanned_at||'-')}</td></tr>`);
    });
    if(!dbs.length) dbt.insertAdjacentHTML('beforeend', `<tr><td colspan="10" class="muted">no DB profiles yet — trigger a DB scan, or wait for the executor to push one.</td></tr>`);
  }catch(e){ $('dbTbl').querySelector('tbody').innerHTML = `<tr><td colspan="10" class="ev-err">${esc(e)}</td></tr>`; }
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
  loadIacArtifacts();
  loadLegacyDispositions();
}
async function doExecutorScan(){
  const action = $('exAction').value;
  const body = {app_id:$('exApp').value.trim(), repo_url:$('exRepo').value.trim(),
                branch:$('exBranch').value.trim(), action, mode:$('exMode').value,
                executor_id:($('exExecutor').value||'')};
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
  'rehost':'var(--green)','rehost-container':'var(--green)','relocate':'var(--green)',
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
  out.innerHTML = `<span class="spinner"></span> asking the MigraQ for a 7R strategy for ${esc(app_id)}…`;
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
      + (r&&r.raw ? `<details class="mcard" style="margin-top:4px;padding:4px 8px"><summary class="muted">raw MigraQ output</summary><pre>${esc((r.raw||'').slice(0,800))}</pre></details>` : '');
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

/* ---------- Manage executors (registry: one default + N named) ---------- */
function _execBadge(s){
  if(!s) return '<span class="muted">—</span>';
  const c = s.reachable ? 'var(--green)' : (s.configured ? 'var(--amber)' : 'var(--fg)');
  const lbl = s.reachable ? `● up${s.version?` · ${esc(s.version)}`:''}`
             : s.configured ? '● down' : '○ unset';
  return `<span style="color:${c}">${lbl}</span>`;
}
async function loadExecutors(){
  const el = $('execList'); if(!el) return;
  el.innerHTML = '<span class="spinner"></span>';
  let list; try{ list = await api('/executors'); }
  catch(e){ el.innerHTML = `<span class="ev-err">load failed: ${esc(e)}</span>`; return; }
  if(!list.length){ el.innerHTML = '<span class="muted">no executors — set IDC_EXECUTOR_URL or add one below.</span>'; return; }
  el.innerHTML = list.map(_execRow).join('');
}
function _execRow(e){
  const id = e.id;
  const tag = e.default ? '<span class="tag" style="color:var(--accent);border-color:var(--accent)">default</span>' : '';
  const del = e.default ? '' : `<button class="sm" onclick="deleteExecutor('${attr(id)}')">delete</button>`;
  const pub = e.default ? `<div class="row" style="flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px">
      <input id="ex_${attr(id)}_pub" value="${attr(e.public_url||'')}" placeholder="this server's public URL (https://mig.zaymuc.com)" style="flex:1 1 360px;min-width:240px" title="IDC_PUBLIC_URL — the address every executor pushes back to over the internet"/>
      <span class="muted" style="font-size:12px">public URL = push-back target for ALL executors</span>
    </div>` : '';
  return `<div class="mcard" style="margin:6px 0;padding:8px 10px">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
      <b>${esc(id)}</b> ${tag} ${_execBadge(e.status)}
      <span class="muted" style="font-size:12px">${esc((e.status||{}).detail||'')}</span>
    </div>
    <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center">
      <input id="ex_${attr(id)}_url" value="${attr(e.url||'')}" placeholder="executor URL (https://...)" style="flex:1 1 260px;min-width:180px"/>
      <input id="ex_${attr(id)}_tok" type="password" placeholder="token ${e.token_set?'(set · blank=keep)':'(unset)'}" style="flex:0 1 150px;min-width:120px"/>
      <label class="muted" style="white-space:nowrap;font-size:12px"><input type="checkbox" id="ex_${attr(id)}_en" ${e.enabled?'checked':''}> enabled</label>
      <input id="ex_${attr(id)}_to" type="number" min="1" value="${e.timeout||600}" style="width:70px" title="timeout (s)"/>
      <button class="sm primary" onclick="saveExecutor('${attr(id)}')">Save</button>
      <button class="sm" onclick="testExecutor('${attr(id)}')">Test</button>
      ${del}
    </div>
    ${pub}
    <div class="out" id="ex_${attr(id)}_out" style="margin-top:4px"></div>
  </div>`;
}
function _execBody(id){
  const body = {url:$(`ex_${id}_url`).value.trim(), enabled:$(`ex_${id}_en`).checked,
                timeout:parseInt($(`ex_${id}_to`).value,10)||600};
  const tok=$(`ex_${id}_tok`).value;
  if(tok) body.token=tok;   // blank = keep current
  if(id==='default'){ const pub=$(`ex_${id}_pub`); if(pub) body.public_url=pub.value.trim(); }
  return body;
}
async function saveExecutor(id){
  const out=$(`ex_${id}_out`); out.innerHTML='<span class="spinner"></span> saving…';
  try{
    const path = id==='default' ? '/executor/config' : '/executors/'+encodeURIComponent(id);
    await api(path, {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(_execBody(id))});
    out.innerHTML = `<span style="color:var(--green)">✓ saved</span>`;
    loadExecutors(); populateExecPickers(); loadExecStatus();
    toast('executor saved','ok');
  }catch(e){ out.innerHTML=`<span class="ev-err">save failed: ${esc(e)}</span>`; }
}
async function testExecutor(id){
  const out=$(`ex_${id}_out`); out.innerHTML='<span class="spinner"></span> testing…';
  try{
    const s = id==='default'
      ? await api('/executor/test', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(_execBody(id))})
      : await api('/executors/'+encodeURIComponent(id)+'/test', {method:'POST'});
    const c = s.reachable ? 'var(--green)' : 'var(--amber)';
    out.innerHTML = `<span style="color:${c}">${s.reachable?`✓ reachable${s.version?` · ${esc(s.version)}`:''}`:`✗ ${esc(s.detail||'unreachable')}`}</span>`;
  }catch(e){ out.innerHTML=`<span class="ev-err">test failed: ${esc(e)}</span>`; }
}
async function deleteExecutor(id){
  if(!confirm(`Delete executor "${id}"?`)) return;
  try{
    await api('/executors/'+encodeURIComponent(id), {method:'DELETE'});
    toast('executor deleted','ok');
    loadExecutors(); populateExecPickers();
  }catch(e){ toast('delete failed: '+e,'err'); }
}
async function addExecutor(){
  const id=$('newExecId').value.trim(), url=$('newExecUrl').value.trim();
  if(!id || !url){ toast('id and url required','warn'); return; }
  const body={url, enabled:$('newExecEnabled').checked, timeout:parseInt($('newExecTimeout').value,10)||600};
  const tok=$('newExecToken').value; if(tok) body.token=tok;
  try{
    await api('/executors/'+encodeURIComponent(id), {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast('executor added','ok');
    $('newExecId').value=''; $('newExecUrl').value=''; $('newExecToken').value='';
    loadExecutors(); populateExecPickers();
  }catch(e){ toast('add failed: '+e,'err'); }
}
async function populateExecPickers(){
  let list; try{ list = await api('/executors'); }catch(e){ return; }
  const opts = list.map(e=>`<option value="${attr(e.id)}">${esc(e.id)}${e.default?' (default)':''}${e.enabled?'':' · off'}</option>`).join('');
  document.querySelectorAll('select.execPicker').forEach(sel=>{
    const cur=sel.value; sel.innerHTML=opts; if(cur && [...sel.options].some(o=>o.value===cur)) sel.value=cur;
    else if(list.length) sel.value=list[0].id;
  });
}

/* ---------- F5/F6 — DB conversion profiles + convert mode ---------- */
function _gradeColor(g){ return g==='A'?'var(--green)':g==='B'?'var(--amber)':g==='C'?'var(--red)':'var(--fg)'; }
async function doDbScan(mode){
  const db_server_id = $('dbScanId').value.trim();
  if(!db_server_id){ toast('enter a db hostname first','warn'); return; }
  const body = { db_server_id, source_engine:$('dbSrcEng').value, target_engine:$('dbTgtEng').value,
                 mode: mode || 'assess', executor_id:($('dbExecutor').value||'') };
  try{
    await api('/db-scan', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast(`DB ${body.mode} requested — profile arrives via callback; Rebuild folds the grade in.`, 'ok');
    setTimeout(loadCode, 1500);
  }catch(e){ toast('DB scan failed: '+e, 'err'); }
}
async function showDbConv(dbServerId){
  const el = $('dbConvDetail');
  el.classList.remove('hidden');
  el.innerHTML = '<span class="spinner"></span>';
  try{
    const d = await api('/db-profiles/'+encodeURIComponent(dbServerId));
    if(!d.conversion){ el.innerHTML = '<span class="muted">no conversion artifact (run Convert).</span>'; return; }
    const c = d.conversion;
    const rows = (c.objects||[]).map(o=>{
      const col = o.status==='auto_converted'?'var(--green)':o.status==='manual_review'?'var(--amber)':o.status==='blocked'?'var(--red)':'var(--fg)';
      return `<tr><td>${esc(o.name)}</td><td>${esc(o.kind||'-')}</td><td style="color:${col};font-weight:600">${esc(o.status)}</td><td>${esc(o.issue||'—')}</td><td>${o.effort_days!=null?o.effort_days:'-'}</td></tr>`;
    }).join('');
    const ddl = (c.ddl||[]).length ? `<details class="mcard" style="margin-top:6px"><summary class="muted">converted DDL (${c.ddl.length} statement(s))</summary><pre>${esc((c.ddl||[]).join('\n'))}</pre></details>` : '';
    el.innerHTML = `<div><b>${esc(d.db_server_id)}</b> → ${esc(c.target_engine||'?')}  ·  ${(c.auto_convert_pct!=null?(c.auto_convert_pct*100).toFixed(0):'?')}% auto</div>
      <div class="xscroll" style="margin-top:6px"><table class="tbl mcard"><thead><tr><th>object</th><th>kind</th><th>status</th><th>issue</th><th>effort(d)</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${ddl}
      ${c.report_md?`<details class="mcard" style="margin-top:6px"><summary class="muted">compatibility report (markdown)</summary><pre>${esc(c.report_md)}</pre></details>`:''}`;
  }catch(e){ el.innerHTML = '<span class="ev-err">'+esc(e)+'</span>'; }
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
  const body = { app_id, server_id, inventory: inv, mode:$('rtMode').value,
                 executor_id:($('rtExecutor').value||'') };
  try{
    await api('/runtime-containerize', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast('Runtime containerize requested — profile arrives via callback (source=runtime-derived).', 'ok');
    setTimeout(loadCode, 1500);
  }catch(e){ toast('runtime-containerize failed: '+e, 'err'); }
}

/* ---------- F5 — IaC + Well-Architected guardrails ---------- */
async function loadIacArtifacts(){
  const tb = $('iacTbl'); if(!tb) return;
  const body = tb.querySelector('tbody'); body.innerHTML='';
  try{
    const arts = await api('/iac-artifacts');
    arts.forEach(a=>{
      const fail = (a.guardrails||[]).filter(g=>g.status==='fail' && (g.severity==='high'||g.severity==='medium')).length;
      const pass = a.guardrail_pass;
      const tag = pass ? `<span style="color:var(--green)">✓ pass</span>`
                       : `<span style="color:var(--red)">✗ ${fail} blocking fail(s)</span>`;
      body.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="scope_id">${esc(a.scope_id||'-')}</td>
        <td data-label="scope">${esc(a.scope||'-')}</td>
        <td data-label="guardrails">${tag} (${(a.guardrails||[]).length})</td>
        <td data-label="modules">${(a.modules||[]).length}</td>
        <td data-label="scanned">${esc(a.scanned_at||'-')}</td>
        <td data-label="show"><button class="sm" onclick="showIac('${attr(a.scope_id)}')">show</button></td></tr>`);
    });
    if(!arts.length) body.insertAdjacentHTML('beforeend', `<tr><td colspan="6" class="muted">no IaC artifacts yet — emit one (e.g. scope=landing_zone, id=lz:corp).</td></tr>`);
  }catch(e){ body.innerHTML = `<tr><td colspan="6" class="ev-err">${esc(e)}</td></tr>`; }
}
async function doIacEmit(){
  const scope = $('iacScope').value;
  const scope_id = $('iacScopeId').value.trim();
  if(!scope_id){ toast('enter a scope_id (lz:corp or wl:<server_id>)','warn'); return; }
  try{
    await api('/iac-emit', {method:'POST', headers:{'content-type':'application/json'},
            body:JSON.stringify({scope, scope_id, context:{}, executor_id:($('iacExecutor').value||'')})});
    toast('IaC emit requested — artifact + guardrails arrive via callback.', 'ok');
    setTimeout(loadIacArtifacts, 1500);
  }catch(e){ toast('iac-emit failed: '+e, 'err'); }
}
async function showIac(scopeId){
  const el = $('iacDetail'); el.classList.remove('hidden'); el.innerHTML='<span class="spinner"></span>';
  try{
    const a = await api('/iac-artifacts/'+encodeURIComponent(scopeId));
    const gr = (a.guardrails||[]).map(g=>{
      const c = g.status==='pass'?'var(--green)':g.status==='fail'?'var(--red)':'var(--amber)';
      return `<tr><td style="color:${c};font-weight:600">${esc(g.status)}</td><td>${esc(g.pillar||'-')}</td><td>${esc(g.rule||'-')}</td><td>${esc(g.finding||'—')}</td><td>${esc(g.severity||'-')}</td></tr>`;
    }).join('');
    const mods = (a.modules||[]).map(m=>`<details class="mcard"><summary>${esc(m.path)}</summary><pre>${esc(m.content||'')}</pre></details>`).join('');
    el.innerHTML = `<div><b>${esc(a.scope_id)}</b>  ·  ${a.guardrail_pass?'<span style="color:var(--green)">guardrails PASS</span>':'<span style="color:var(--red)">guardrails FAIL</span>'}  ·  ${esc(a.plan_summary||'')}</div>
      <div class="xscroll" style="margin-top:6px"><table class="tbl mcard"><thead><tr><th>status</th><th>pillar</th><th>rule</th><th>finding</th><th>severity</th></tr></thead><tbody>${gr}</tbody></table></div>
      <div style="margin-top:6px">${mods||'<span class="muted">no modules</span>'}</div>`;
  }catch(e){ el.innerHTML='<span class="ev-err">'+esc(e)+'</span>'; }
}

/* ---------- F7 — legacy / unsupported-OS disposition ---------- */
const _LD_COLOR = {containerize:'var(--green)', replatform:'#3b8eea', rewrite:'var(--amber)', retain:'#888', retire:'#e5484d'};
async function loadLegacyDispositions(){
  const tb = $('ldTbl'); if(!tb) return;
  const body = tb.querySelector('tbody'); body.innerHTML='';
  try{
    const ds = await api('/legacy-dispositions');
    ds.forEach(d=>{
      body.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="server_id">${esc(d.server_id||'-')}</td>
        <td data-label="disposition"><span style="color:${_LD_COLOR[d.disposition]||'var(--fg)'};font-weight:600">${esc(d.disposition||'-')}</span></td>
        <td data-label="confidence">${d.confidence!=null?d.confidence.toFixed(2):'-'}</td>
        <td data-label="effort">${d.effort_days!=null?d.effort_days:'-'}</td>
        <td data-label="base_image">${esc(d.target_base_image||'-')}</td>
        <td data-label="rationale" class="conf" title="${esc(d.rationale||'')}">${esc((d.rationale||'-').slice(0,60))}</td>
        <td data-label="scanned">${esc(d.scanned_at||'-')}</td></tr>`);
    });
    if(!ds.length) body.insertAdjacentHTML('beforeend', `<tr><td colspan="7" class="muted">no dispositions yet — analyze an EOL host (e.g. role=app, os=centos 6, no repo → containerize).</td></tr>`);
  }catch(e){ body.innerHTML = `<tr><td colspan="7" class="ev-err">${esc(e)}</td></tr>`; }
}
async function doLegacyAnalyze(){
  const server_id = $('ldServer').value.trim();
  if(!server_id){ toast('enter a server_id first','warn'); return; }
  const ctx = { role:$('ldRole').value, os:$('ldOs').value.trim(),
                runtime:$('ldRuntime').value.trim(),
                has_source_repo:$('ldHasRepo').checked, os_eol_bucket:'expired' };
  try{
    await api('/legacy-disposition', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({server_id, context:ctx, executor_id:($('ldExecutor').value||'')})});
    toast('Legacy-disposition analysis requested — arrives via callback.', 'ok');
    setTimeout(loadLegacyDispositions, 1500);
  }catch(e){ toast('legacy-disposition failed: '+e, 'err'); }
}
