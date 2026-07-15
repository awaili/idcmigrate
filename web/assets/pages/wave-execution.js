/* ---------- Wave Execution tab (F6 state machine + F7 rollup) ---------- */
const _EXEC_STATUS_COLOR = {
  planned: '#888', running: '#3b8eea', done: 'var(--green)',
  'rolled-back': 'var(--amber)', 'not_launched': '#888',
  replicating: '#3b8eea', tested: '#3b8eea', ready_for_cutover: '#a06bff',
  cut_over: '#a06bff', finalized: 'var(--green)', rolled_back: 'var(--amber)',
};
function _statusBadge(s){ const c = _EXEC_STATUS_COLOR[s]||'var(--fg)'; return `<span class="tag" style="color:${c}">${esc(s||'-')}</span>`; }
let _execSelectedWave = null;

async function loadExecution(){
  try{
    const waves = await api('/waves');
    if(!waves.length){ $('execWaves').innerHTML = '<span class="muted">no waves — run Rebuild first</span>'; return; }
    $('execWaves').innerHTML = waves.map(w=>{
      const dom = (w.execution&&w.execution.dominant_status) || 'planned';
      return `<div class="mcard" style="padding:8px 10px;margin-top:6px">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div><b>${esc(w.name||w.id)}</b> <span class="muted">${esc(w.stage||'')} · ${w.server_ids.length} servers</span>
            ${_statusBadge(dom)}</div>
          <div class="row" style="gap:6px">
            <button class="sm primary" onclick="execLaunch('${esc(w.id)}')">Launch jobs</button>
            <button class="sm" onclick="execView('${esc(w.id)}')">View</button>
            <button class="sm" onclick="execHandoff('${esc(w.id)}')">Handoff CSV</button>
          </div>
        </div></div>`;
    }).join('');
    if(_execSelectedWave) execView(_execSelectedWave);
  }catch(e){ $('execWaves').innerHTML = '<span class="ev-err">'+esc(String(e))+'</span>'; }
  loadPostmig();
}

/* ---------- F10 — post-migration optimization ---------- */
async function loadPostmig(){
  const tb = $('pmTbl'); if(!tb) return;
  const body = tb.querySelector('tbody'); body.innerHTML='';
  try{
    const [recs, sav] = await Promise.all([api('/postmig-recs'), api('/postmig-savings')]);
    $('pmSavings').textContent = `total savings: $${sav.monthly_saving_usd}/mo · $${sav.yearly_saving_usd}/yr (${sav.rec_count} recs)`;
    const kcol = {right_size:'#3b8eea', reserved:'var(--green)', anomaly:'var(--amber)', perf:'var(--amber)'};
    recs.forEach(r=>{
      const ft = (r.from_spec||r.to_spec) ? `${esc(r.from_spec||'-')}→${esc(r.to_spec||'-')}` : '-';
      body.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="server_id">${esc(r.server_id||'-')}</td>
        <td data-label="kind"><span style="color:${kcol[r.kind]||'var(--fg)'};font-weight:600">${esc(r.kind||'-')}</span></td>
        <td data-label="from→to">${ft}</td>
        <td data-label="saving">${r.monthly_saving_usd?('$'+r.monthly_saving_usd):'-'}</td>
        <td data-label="conf">${r.confidence!=null?r.confidence.toFixed(2):'-'}</td>
        <td data-label="severity">${esc(r.severity||'-')}</td>
        <td data-label="reason" class="conf" title="${esc(r.reason||'')}">${esc((r.reason||'-').slice(0,60))}</td></tr>`);
    });
    if(!recs.length) body.insertAdjacentHTML('beforeend', `<tr><td colspan="7" class="muted">no post-mig recs yet — analyze a finalized host.</td></tr>`);
  }catch(e){ body.innerHTML = `<tr><td colspan="7" class="ev-err">${esc(e)}</td></tr>`; }
}
async function doPostmigScan(){
  const server_id = $('pmServer').value.trim();
  if(!server_id){ toast('enter a server_id','warn'); return; }
  const ctx = {target:{product:$('pmProduct').value, spec:$('pmSpec').value.trim()},
               metrics:{cpu_p95:8, mem_p95:22, uptime_pct:99.5}};
  try{
    await api('/postmig-optimize', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({server_id, context:ctx})});
    toast('post-mig analysis requested — recs arrive via callback.', 'ok');
    setTimeout(loadPostmig, 1500);
  }catch(e){ toast('post-mig scan failed: '+e, 'err'); }
}

async function execLaunch(wid){
  try{
    await api(`/waves/${wid}/execute?kind=host`, {method:'POST'});
    toast('launched migration jobs', 'ok'); execView(wid);
  }catch(e){ toast('launch failed: '+e, 'err'); }
}

async function execHandoff(wid){
  // download the CSV directly
  window.open(API+`/waves/${wid}/handoff.csv`, '_blank');
}

async function execView(wid){
  _execSelectedWave = wid;
  try{
    const roll = await api(`/waves/${wid}/execution`);
    if(roll.error){ $('execDetail').innerHTML = `<span class="ev-err">${esc(roll.error)}</span>`; return; }
    const sc = roll.status_counts||{};
    const scLine = Object.entries(sc).filter(([k,v])=>v>0).map(([k,v])=>`${k}×${v}`).join(' · ') || '-';
    const rows = roll.per_server||[];
    const tbody = rows.map(r=>`<tr>
      <td>${esc(r.hostname||r.server_id)}</td>
      <td>${esc((r.app_ids||[]).join(',')||'-')}</td>
      <td>${esc(r.target||'-')} <span class="muted">${esc(r.region||'')}</span></td>
      <td>${esc(r.kind||'-')}</td>
      <td>${_statusBadge(r.status)}</td>
      <td class="row" style="gap:4px">
        <button class="sm" onclick="execAdvance('${esc(r.migration_job_id)}','replicating')">→rep</button>
        <button class="sm" onclick="execAdvance('${esc(r.migration_job_id)}','tested')">→test</button>
        <button class="sm" onclick="execAdvance('${esc(r.migration_job_id)}','ready_for_cutover')">→ready</button>
        <button class="sm" onclick="execAdvance('${esc(r.migration_job_id)}','cut_over')">→cut</button>
        <button class="sm" onclick="execValidate('${esc(r.migration_job_id)}')">validate</button>
        <button class="sm" onclick="execComplete('${esc(r.migration_job_id)}')">complete</button>
        <button class="sm" onclick="execRevert('${esc(r.migration_job_id)}')" title="revert to rolled_back — valid from any non-terminal state">rollback</button>
      </td></tr>`).join('') || '<tr><td class="muted" colspan=6>launch jobs for this wave first</td></tr>';
    $('execDetail').innerHTML = `
      <h2>${esc(roll.wave_name||wid)} <span class="muted">${esc(roll.wave_stage||'')} · ${roll.server_count} servers · dominant ${_statusBadge(roll.dominant_status)}</span></h2>
      <div class="muted" style="margin-bottom:6px">${esc(scLine)}</div>
      <div class="xscroll"><table class="tbl"><thead><tr>
        <th>hostname</th><th>app</th><th>target</th><th>kind</th><th>status</th><th>actions</th>
      </tr></thead><tbody>${tbody}</tbody></table></div>`;
  }catch(e){ $('execDetail').innerHTML = '<span class="ev-err">'+esc(String(e))+'</span>'; }
}

async function execAdvance(jobId, to){
  if(!jobId) return;
  try{ await api(`/migration-jobs/${jobId}/advance`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({to})}); }
  catch(e){ toast('advance failed: '+e, 'err'); }
  if(_execSelectedWave) execView(_execSelectedWave);
}
async function execValidate(jobId){
  if(!jobId) return;
  try{ const r = await api(`/migration-jobs/${jobId}/validate`, {method:'POST'}); toast(`gates ${r.all_must_pass_ok?'OK':'NOT satisfied'}`, r.all_must_pass_ok?'ok':'err'); }
  catch(e){ toast('validate failed: '+e, 'err'); }
  if(_execSelectedWave) execView(_execSelectedWave);
}
async function execComplete(jobId){
  if(!jobId) return;
  try{ await api(`/migration-jobs/${jobId}/complete`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({})}); toast('marked complete', 'ok'); }
  catch(e){ toast('complete failed: '+e, 'err'); }
  if(_execSelectedWave) execView(_execSelectedWave);
}
async function execRevert(jobId){
  if(!jobId) return;
  if(!confirm('Revert this migration job to rolled_back? Valid from any non-terminal state.')) return;
  try{ await api(`/migration-jobs/${jobId}/revert`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({to:'rolled_back',by:'web'})}); toast('reverted to rolled_back', 'ok'); }
  catch(e){ toast('revert failed: '+e, 'err'); }
  if(_execSelectedWave) execView(_execSelectedWave);
}