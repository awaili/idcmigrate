/* ---------- server drawer ---------- */
const _7R_COLORS = {'rehost':'var(--green)','rehost-container':'var(--green)',
  'replatform':'#3b8eea','refactor':'var(--amber)','repurchase':'#a06bff',
  'retain':'#888','retire':'#e5484d'};
function _stratBadge(s){ if(!s) return '<span class="muted">-</span>'; return `<span style="color:${_7R_COLORS[s]||'var(--fg)'};font-weight:600">${esc(s)}</span>`; }
function _coverageBadge(s, m){
  // F4 — data-coverage confidence + sizing basis + final match confidence
  const cov = s.assessment_confidence==null ? null : s.assessment_confidence;
  const pct = cov==null ? '-' : (cov*100).toFixed(0)+'%';
  const basis = s.sizing_basis || '-';
  const basisColor = s.sizing_basis==='measured' ? 'var(--green)' : (s.sizing_basis==='estimated' ? 'var(--amber)' : 'var(--fg)');
  const conf = (m && m.confidence!=null) ? ` <span class="muted">· match conf ${m.confidence.toFixed(2)}</span>` : '';
  return `<span>${pct}</span> <span class="tag" style="color:${basisColor}">${esc(basis)} sizing</span>${conf}`;
}
function _warrantyBadge(s){
  // backend precomputes s.warranty_bucket (match.warranty_bucket) so we render
  // that directly; fall back to deriving from the raw fields only if absent
  // (e.g. a stale row from before the field shipped).
  let b = s.warranty_bucket;
  if(!b){
    const ws=(s.warranty_status||'').toLowerCase().trim();
    const eol=(s.hardware_eol||'').trim();
    const today=new Date().toISOString().slice(0,10);
    b='unknown';
    if(['active','expiring','expired','unknown'].includes(ws)) b=ws;
    else if(eol){
      const days=(new Date(eol)-new Date(today))/86400000;
      b = days<0 ? 'expired' : (days<=90 ? 'expiring' : 'active');
    }
  }
  const c=_BUCKET_COLOR[b]||'var(--fg)';
  const eolTxt=(s.hardware_eol||'').trim()?` <span class="muted">· EOL ${esc(s.hardware_eol)}</span>`:'';
  return `<span class="tag" style="color:${c}">${b}</span>${eolTxt}`;
}
function _osEolBadge(s){
  // s.os_eol_bucket precomputed by the backend (eol.os_eol_bucket) against the
  // bundled EOL table — the silent-rehost-of-CentOS-6 flag.
  const b = s.os_eol_bucket || 'unknown';
  const c=_BUCKET_COLOR[b]||'var(--fg)';
  return `<span class="tag" style="color:${c}">${b}</span> <span class="muted">${esc(s.os||'-')} ${esc(s.os_version||'')}</span>`;
}
function _codeSection(code){
  if(!code || !code.length) return '';
  const cards = code.map(c=>{
    const cats = Object.entries(c.finding_categories||{}).map(([k,v])=>`<span class="tag">${esc(k)}×${v}</span>`).join(' ')||'-';
    const ends = (c.network_endpoints||[]).slice(0,8).map(e=>`<span class="tag">${esc(e)}</span>`).join(' ')||'-';
    const deps = (c.code_deps||[]).slice(0,8).map(d=>`<span class="tag">${esc(d)}</span>`).join(' ')||'-';
    const blockers = (c.blockers||[]).length ? `<div style="color:var(--amber)">⚠ blockers: ${esc(c.blockers.join('; '))}</div>` : '';
    return `<div class="mcard" style="padding:6px 8px;margin-top:6px">
      <div class="row" style="justify-content:space-between;align-items:baseline">
        <b>${esc(c.app_id)}</b>
        <span class="muted">${esc(c.language||'-')} · ${esc(c.runtime||'-')} · ${esc(c.framework||'-')}</span>
      </div>
      <div class="row" style="gap:14px;flex-wrap:wrap;font-size:13px;margin:3px 0">
        <span><span class="muted">executor pattern</span> ${_stratBadge(c.migration_pattern)}</span>
        ${c.ai_strategy?`<span><span class="muted">AI 7R</span> ${_stratBadge(c.ai_strategy.strategy)} <span class="muted">→ ${esc(c.ai_strategy.target||'-')} (conf ${c.ai_strategy.confidence!=null?c.ai_strategy.confidence.toFixed(2):'-'})</span></span>`:''}
        <span><span class="muted">effort</span> ${esc((c.ai_strategy||{}).effort||c.refactor_effort||'-')}</span>
        <span><span class="muted">readiness</span> ${c.cloud_readiness==null?'-':c.cloud_readiness.toFixed(2)}</span>
        <span><span class="muted">findings</span> ${c.findings_count||0}</span>
        <span><span class="muted">changes</span> ${c.required_changes_count||0}</span>
      </div>
      ${c.ai_strategy&&c.ai_strategy.rationale?`<div class="muted" style="font-size:11px;margin:2px 0">AI: ${esc(c.ai_strategy.rationale)}</div>`:''}
      <div class="muted" style="font-size:11px;margin-bottom:2px">finding categories</div><div style="margin-bottom:4px">${cats}</div>
      <div class="muted" style="font-size:11px;margin-bottom:2px">calls (code-discovered endpoints)</div><div style="margin-bottom:4px">${ends}</div>
      <div class="muted" style="font-size:11px;margin-bottom:2px">code deps (other apps)</div><div style="margin-bottom:4px">${deps}</div>
      ${blockers}
      ${c.summary?`<div class="muted" style="font-size:11px;margin-top:3px">${esc(c.summary)}</div>`:''}
      <div class="row" style="margin-top:4px;gap:6px"><button class="sm primary" onclick="drawerSevenR('${esc(c.app_id)}')">7R strategy</button><span class="muted" style="font-size:11px">scanned ${esc(c.scanned_at||'-')}</span></div>
    </div>`;
  }).join('');
  return `<div style="margin-top:8px"><div class="muted" style="font-size:12px;margin-bottom:2px">Code scan (executor feedback — migration-strategy reference)</div>${cards}</div>`;
}
function openServer(id){
  // fetch fresh full record (the row in state.last may be a partial list row)
  fetch(API+'/servers/'+id).then(r=>r.json()).then(s=>{
    const m=s.match||{}, u=s.utilization||{};
    $('dTitle').textContent=s.hostname;
    const kv = (label, val) => `<div class="kv-row"><span class="kv-label">${label}</span><span class="kv-val">${val}</span></div>`;
    $('dBody').innerHTML=`
      <div class="muted">${s.fqdn||''} · ${s.source_type} · ${s.role}</div>
      <div class="kv">
        ${kv('OS', `${s.os||'-'} ${s.os_version||''}`)}
        ${kv('CPU / RAM', `${s.cpu_cores||'-'} vCPU / ${s.mem_gb||'-'} GB`)}
        ${kv('Disks', (s.disks||[]).map(d=>`${d.name} ${d.size_gb}GB ${d.kind} (${d.fs})`).join('<br>')||'-')}
        ${kv('Network', `${(s.ips||[]).join(', ')||'-'} · ${s.subnet||'-'} · ${s.vlan||'-'}`)}
        ${kv('Env / Crit', `${s.env||'-'} <span class="pill ${s.business_criticality||''}">${s.business_criticality||'-'}</span>`)}
        ${kv('Apps', (s.app_ids||[]).join(', ')||'-')}
        ${kv('Tags', (s.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join(' ')||'-')}
        ${kv('Utilization', `cpu ${u.cpu_p95??'-'}% · mem ${u.mem_p95??'-'}% · disk ${u.disk_used_pct??'-'}% <span class="muted">(${u.source||'-'})</span>`)}
        ${kv('Provenance', (s.source_refs||[]).map(r=>`<span class="tag">${esc(r.source)}:${esc(r.source_id)}</span>`).join(' ')||'-')}
        ${kv('Coverage', _coverageBadge(s, m))}
        ${kv('Warranty', _warrantyBadge(s))}
        ${kv('OS support', _osEolBadge(s))}
        ${kv('Target', `<b>${m.target?m.target.product:'-'}</b> ${m.target?esc(m.target.spec):''} <span class="muted">@ ${m.target?m.target.region:''}</span>`)}
        ${kv('Rule', m.rationale?esc(m.rationale):'-')}
      </div>
      ${_codeSection(s.code)}
      <div class="row"><button class="primary" onclick="explainServer('${s.id}')">LLM explain match</button><button onclick="rightSize('${s.id}')">right-size</button><button onclick="drawerAudit('${s.id}')">audit target</button><button onclick="setWarranty('${s.id}')">set warranty</button></div>
      <pre id="dExplain" style="margin-top:10px"></pre>`;
    openDrawer();
  }).catch(e=>{ $('dTitle').textContent='Error'; $('dBody').innerHTML='<span class="ev-err">'+esc(String(e))+'</span>'; openDrawer(); });
}
function closeDrawer(){ $('drawer').classList.remove('open'); $('drawerBackdrop').classList.remove('open'); }
function openDrawer(){ $('drawer').classList.add('open'); $('drawerBackdrop').classList.add('open'); }
document.addEventListener('keydown', e=>{ if(e.key==='Escape' && $('drawer').classList.contains('open')) closeDrawer(); });
async function explainServer(id){ $('dExplain').innerHTML='<span class="spinner"></span>'; try{ const r=await api('/explain',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({server_id:id})}); $('dExplain').textContent=r.explanation; }catch(e){ $('dExplain').textContent='Error: '+e; } }
async function rightSize(id){ $('dExplain').innerHTML='<span class="spinner"></span>'; try{ const r=await api('/right-size',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({server_id:id})}); $('dExplain').textContent=r.advice; }catch(e){ $('dExplain').textContent='Error: '+e; } }
async function drawerSevenR(appId){
  $('dExplain').innerHTML = `<span class="spinner"></span> asking the LLM for a 7R strategy for ${esc(appId)}…`;
  try{
    const r = await api('/strategy', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({app_id:appId})});
    if(!r.ok){ $('dExplain').innerHTML = `<span class="ev-err">7R failed: ${esc(r.error||'unknown')}</span>`; return; }
    const kc = (r.key_changes||[]).map(k=>`  • ${esc(k)}`).join('\n');
    $('dExplain').textContent =
      `${r.app_id} → ${r.strategy} (target ${r.target||'-'}, effort ${r.effort||'-'}, conf ${r.confidence!=null?r.confidence.toFixed(2):'-'})\n`+
      `${r.rationale||''}\n` + (kc?`key changes:\n${kc}`:'');
  }catch(e){ $('dExplain').innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; }
}
async function drawerAudit(id){
  $('dExplain').innerHTML = '<span class="spinner"></span> auditing the rule-based target…';
  try{
    const r = await api('/match/audit', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({server_id:id})});
    if(!r.ok){ $('dExplain').innerHTML = `<span class="ev-err">audit failed: ${esc(r.error||'unknown')}</span>`; return; }
    const color = {keep:'var(--green)', change:'var(--amber)', review:'#a06bff'}[r.verdict]||'var(--fg)';
    const alt = r.alternative_target ? `\nSUGGESTED ALTERNATIVE: ${r.alternative_target} ${r.alternative_spec||''}` : '';
    const risks = (r.risks||[]).length ? `\nrisks:\n${r.risks.map(x=>'  • '+x).join('\n')}` : '';
    $('dExplain').textContent =
      `rule target: ${r.rule_target.product} ${r.rule_target.spec||''} (conf ${r.rule_target.confidence!=null?r.rule_target.confidence.toFixed(2):'-'})\n`+
      `VERDICT: ${r.verdict.toUpperCase()} (audit conf ${r.confidence!=null?r.confidence.toFixed(2):'-'})${alt}\n`+
      `${r.critique||''}\n${r.rationale||''}${risks}`;
    // color the verdict line by injecting a leading marker
    $('dExplain').insertAdjacentHTML('afterbegin', `<span style="color:${color}">●</span> `);
  }catch(e){ $('dExplain').innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; }
}
async function setWarranty(id){
  // operator override of hardware support status (data-gap) — a small inline
  // form (not sequential prompt() dialogs) so the operator sees both fields at
  // once. Renders into dExplain; Save PUTs + re-opens the drawer to refresh.
  $('dExplain').innerHTML = `
    <div class="mcard" style="padding:10px">
      <div style="font-weight:600;margin-bottom:8px">Set hardware support status</div>
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
        <label style="font-size:13px">warranty_status
          <select id="warrStatus" style="margin-left:4px">
            <option value="">(derive from EOL)</option>
            <option value="active">active</option>
            <option value="expiring">expiring</option>
            <option value="expired">expired</option>
            <option value="unknown">unknown</option>
          </select>
        </label>
        <label style="font-size:13px">hardware_eol
          <input type="date" id="warrEol" style="margin-left:4px" />
        </label>
        <button class="sm primary" onclick="saveWarranty('${id}')">Save</button>
        <button class="sm" onclick="document.getElementById('dExplain').innerHTML=''">Cancel</button>
      </div>
      <div class="muted" style="font-size:11px;margin-top:8px">Leave warranty_status blank to derive the bucket from the EOL date. Empty EOL clears it. Persists immediately (no rebuild needed).</div>
    </div>`;
}
async function saveWarranty(id){
  const ws=$('warrStatus').value, eol=$('warrEol').value;
  try{
    const r = await api('/servers/'+id+'/warranty', {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify({warranty_status:ws, hardware_eol:eol})});
    toast(`warranty set: ${r.warranty_status||'(derive)'} ${r.hardware_eol||''}`, 'ok');
    $('dExplain').innerHTML='';
    openServer(id);  // re-open to refresh the badge
  }catch(e){ toast('set warranty failed: '+e, 'err'); }
}
