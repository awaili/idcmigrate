/* ---------- waves ---------- */
let WAVES=[];
async function loadWaves(){ WAVES = await api('/waves'); renderWaves(); }

async function reviewPlan(){
  const out = $('planReview'); const hint = $('reviewHint'); if(!out) return;
  out.innerHTML = '<span class="spinner"></span> red-teaming the plan…';
  if(hint) hint.textContent = '';
  try{
    const r = await api('/plan/review', {method:'POST', headers:{'content-type':'application/json'}, body:'{}'});
    if(!r.ok){ out.innerHTML = `<span class="ev-err">review failed: ${esc(r.error||'unknown')}</span>`; return; }
    const color = {sound:'var(--green)', 'needs-work':'var(--amber)', risky:'var(--red)'}[r.overall]||'var(--fg)';
    if(hint) hint.textContent = `${r.overall} · ${r.findings.length} finding(s)`;
    const rows = (r.findings||[]).map(f=>{
      const sc = {high:'var(--red)', medium:'var(--amber)', low:'var(--fg)'}[f.severity]||'var(--fg)';
      return `<div class="mcard" style="padding:5px 8px;margin:4px 0;border-left:3px solid ${sc}">
        <div class="row" style="justify-content:space-between"><b style="color:${sc}">${esc(f.severity)}</b><span class="muted">${esc(f.wave||'-')}</span></div>
        <div>${esc(f.issue)}</div>
        ${f.suggestion?`<div class="muted" style="font-size:12px">→ ${esc(f.suggestion)}</div>`:''}</div>`;
    }).join('') || '<div class="muted">no findings — plan looks sound.</div>';
    out.innerHTML = `<div style="margin-bottom:4px"><span class="muted">overall:</span> <b style="color:${color}">${r.overall}</b> <span class="muted">(${r.wave_count} waves)</span></div>`
      + rows + (r.summary?`<div class="muted" style="margin-top:4px">${esc(r.summary)}</div>`:'');
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; }
}
function renderWaves(){
  $('waveList').innerHTML = WAVES.map(w=>`<div class="wave" onclick="openWave('${w.id}')">
    <div class="row"><span class="name">${esc(w.name)}</span><span class="pill ${w.stage.startsWith('1')?'high':w.stage.startsWith('4')?'medium':'low'}">${w.stage}</span><span style="margin-left:auto" class="meta">${w.members.length} servers</span></div>
    <div class="deps">depends_on: ${w.depends_on.join(', ')||'(none)'}</div>
    <div class="meta">${esc(w.rationale||'')}</div></div>`).join('');
}
let waveState={id:null,page:1,page_size:25};
async function openWave(id){
  waveState={id,page:1,page_size:25};
  // look up the name from the already-loaded WAVES (wave names are LLM-generated
  // and may contain apostrophes, so they must not be inlined into onclick).
  const w = WAVES.find(x=>x.id===id);
  $('waveMembersTitle').textContent = ((w&&w.name)||id) + ' — members';
  await fetchWaveMembers();
}
async function fetchWaveMembers(){
  const p=new URLSearchParams({wave_id:waveState.id,page:waveState.page,page_size:waveState.page_size,order_by:'hostname'});
  const r = await api('/servers?'+p.toString());
  const rows = (r.items||[]).map(s=>{const m=s.match||{};return `<tr onclick="openServer('${s.id}')">
      <td data-label="Hostname"><b>${esc(s.hostname)}</b></td>
      <td data-label="Role"><span class="tag ${s.role}">${s.role}</span></td>
      <td data-label="CPU/Mem">${s.cpu_cores}/${s.mem_gb}G</td>
      <td data-label="Env">${s.env||'-'}</td>
      <td data-label="Target"><b>${m.target?m.target.product:'-'}</b></td>
      <td data-label="Conf" class="conf">${m.confidence?'_'+m.confidence.toFixed(1):'-'}</td>
    </tr>`;}).join('');
  const pages=Math.max(1,Math.ceil(r.total/waveState.page_size));
  $('waveMembers').innerHTML = `<div class="scroll"><table class="mcard"><thead><tr><th>hostname</th><th>role</th><th>cpu/mem</th><th>env</th><th>target</th><th>conf</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="pager"><div class="row"><button class="sm" onclick="waveState.page=Math.max(1,waveState.page-1);fetchWaveMembers()">‹</button><span class="muted">page ${r.page}/${pages} · ${r.total} servers</span><button class="sm" onclick="waveState.page++;fetchWaveMembers()">›</button></div>
    <button class="sm" onclick="window.open('/api/servers.csv?wave_id=${waveState.id}','_blank')">export wave CSV</button>
    <button class="sm primary" onclick="assessWave('${waveState.id}')">assess risk + runbook</button></div>
    <div id="waveAssess" style="margin-top:8px"></div>`;
}
async function assessWave(id){
  const out = $('waveAssess'); if(!out) return;
  out.innerHTML = '<span class="spinner"></span> assessing wave…';
  try{
    const r = await api('/wave/assess', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({server_id:id})});
    const color = {low:'var(--green)', medium:'var(--amber)', high:'var(--red)'}[r.risk_level]||'var(--fg)';
    const gc = {go:'var(--green)', hold:'var(--amber)', 'no-go':'var(--red)'}[r.go_no_go]||'var(--fg)';
    const rb = r.runbook || {};
    const list = (items) => (items&&items.length)?`<ul style="margin:2px 0 6px 18px">${items.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<div class="muted">—</div>';
    out.innerHTML = `
      <div class="row" style="gap:14px;align-items:baseline;flex-wrap:wrap">
        <span><span class="muted">risk</span> <b style="color:${color}">${r.risk_level} (${r.risk_score})</b></span>
        ${r.go_no_go?`<span><span class="muted">go/no-go</span> <b style="color:${gc}">${r.go_no_go}</b></span>`:''}
      </div>
      <div class="muted" style="margin:4px 0">risk factors: ${esc((r.risk_factors||[]).join('; '))}</div>
      ${r.summary?`<div style="margin:4px 0">${esc(r.summary)}</div>`:''}
      <div class="grid2col" style="margin-top:6px">
        <div><div class="muted" style="font-size:11px">pre-checks</div>${list(rb.pre_checks)}</div>
        <div><div class="muted" style="font-size:11px">cutover</div>${list(rb.cutover)}</div>
        <div><div class="muted" style="font-size:11px">rollback</div>${list(rb.rollback)}</div>
      </div>
      ${r.ok?'':`<div class="ev-err" style="margin-top:4px">LLM unavailable: ${esc(r.error||'')} — deterministic risk score above is still valid.</div>`}`;
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; }
}

/* ---------- F8 — Landing Zone archetypes (placement gate) ---------- */
const _LZ_ARCH_COLOR = {corp:'#3b8eea', online:'var(--green)', dmz:'#a06bff'};
const _LZ_STATUS_BADGE = {not_ready:['not ready','var(--amber)'], applied:['applied','#3b8eea'], finalized:['finalized','var(--green)']};
function _lzArchBadge(a){ const c=_LZ_ARCH_COLOR[a]||'var(--fg)'; return `<span class="tag" style="color:${c}">${esc(a)}</span>`; }
async function loadLzReadiness(){
  const out=$('lzArchetypes');
  out.innerHTML='<span class="spinner"></span>';
  try{
    const arch = await api('/lz/archetypes');
    const rdy = await api('/lz/readiness');
    const rows = ['corp','online','dmz'].map(a=>{
      const r = rdy[a]||{}; const sb = _LZ_STATUS_BADGE[r.status||'not_ready'];
      const bp = (arch.archetypes||{})[a]||{};
      const setSt = (s)=>`setLzStatus('${a}','${s}')`;
      return `<div class="mcard" style="padding:10px;margin-top:6px">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div>${_lzArchBadge(a)} <b>${esc(bp.summary||a)}</b>
            <span class="tag" style="color:${sb[1]}">${sb[0]}</span>
            <span class="muted">${r.workload_count||0} workload server(s)</span></div>
          <div class="row" style="gap:6px">
            <button class="sm" onclick="${setSt('applied')}">mark applied</button>
            <button class="sm primary" onclick="${setSt('finalized')}">mark finalized</button>
            <button class="sm" onclick="${setSt('not_ready')}">reset</button>
          </div>
        </div>
        <div class="muted" style="font-size:11px;margin-top:4px">VPC ${esc(bp.vpc&&bp.vpc.name||'-')} · peering ${esc(JSON.stringify((bp.peering||{})))} · policies: ${((bp.policy_as_code||[]).length)}</div>
      </div>`;
    }).join('');
    out.innerHTML = rows || '<span class="muted">no archetypes</span>';
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
}
async function setLzStatus(archetype, status){
  try{ await api(`/lz/${archetype}/status`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({status,by:'web'})}); toast(`${archetype} → ${status}`,'ok'); }
  catch(e){ toast('lz status update failed: '+e,'err'); return; }
  loadLzReadiness();
}
