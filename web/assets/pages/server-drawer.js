/* ---------- server drawer ---------- */
// _7R_COLORS lives in app.js now (shared with code.js — both used to
// redeclare it, which aborted the page script).
function _stratBadge(s){ if(!s) return '<span class="muted">-</span>'; return `<span style="color:${_7R_COLORS[s]||'var(--fg)'};font-weight:600">${esc(s)}</span>`; }
function _disksHtml(disks, u){
  // per-partition list with three columns — Label | Mount | Size — plus a total.
  // Uses divs (not a <table>) so the drawer's mobile card-reflow (which keys off
  // data-label on <td>) doesn't mangle it; the three columns stack cleanly on
  // phones too. disks come from parse_disks:
  //   Windows/VMware: name="Hard disk N", fs=""  -> Label=Hard disk N, Mount=–
  //   Linux:          name=mount path,    fs=mount path -> Label=path, Mount=path
  // (the Linux diskList only carries mount:size, so the label and mount are the
  // same path — the source has no separate filesystem label / device name).
  const ds = disks || [];
  if(!ds.length) return '-';
  const total = ds.reduce((a,d)=>a+(d.size_gb||0), 0);
  const head = `<div style="display:flex;gap:10px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:500;padding-bottom:3px;border-bottom:1px solid var(--border)">
      <span style="flex:1 1 40%">Label</span><span style="flex:1 1 40%">Mount</span><span style="flex:0 0 auto;text-align:right">Size</span></div>`;
  const rows = ds.map(d=>{
    const mount = d.fs ? esc(d.fs) : '<span class="muted">–</span>';
    return `<div style="display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--border)">
        <span style="flex:1 1 40%;word-break:break-word">${esc(d.name||'-')}</span>
        <span style="flex:1 1 40%;word-break:break-word" class="muted">${mount}</span>
        <span class="conf" style="flex:0 0 auto;text-align:right;white-space:nowrap">${d.size_gb} GB</span></div>`;
  }).join('');
  const used = (u && u.disk_used_pct!=null) ? `<span class="muted"> · used ${u.disk_used_pct}%</span>` : '';
  return `<div style="margin-top:2px;text-align:left;width:100%">
      ${head}
      ${rows}
      <div style="display:flex;gap:10px;padding-top:5px"><span style="flex:1 1 40%"></span><span style="flex:1 1 40%"></span><span style="flex:0 0 auto;text-align:right;white-space:nowrap"><b>${total} GB</b>${used}</span></div>
    </div>`;
}
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
    const agentBlockers = (c.agent_blockers||[]).length ? `<div style="color:var(--amber);margin-top:2px">🤖 agent blockers (read from repo, weight HIGHER): ${esc(c.agent_blockers.join('; '))}</div>` : '';
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
        ${(c.agent_findings_count||0)?`<span><span class="muted">🤖 agent</span> ${c.agent_findings_count}</span>`:''}
        <span><span class="muted">changes</span> ${c.required_changes_count||0}</span>
      </div>
      ${c.ai_strategy&&c.ai_strategy.rationale?`<div class="muted" style="font-size:11px;margin:2px 0">AI: ${esc(c.ai_strategy.rationale)}</div>`:''}
      <div class="muted" style="font-size:11px;margin-bottom:2px">finding categories</div><div style="margin-bottom:4px">${cats}</div>
      <div class="muted" style="font-size:11px;margin-bottom:2px">calls (code-discovered endpoints)</div><div style="margin-bottom:4px">${ends}</div>
      <div class="muted" style="font-size:11px;margin-bottom:2px">code deps (other apps)</div><div style="margin-bottom:4px">${deps}</div>
      ${blockers}
      ${agentBlockers}
      ${c.summary?`<div class="muted" style="font-size:11px;margin-top:3px">${esc(c.summary)}</div>`:''}
      <div class="row" style="margin-top:4px;gap:6px"><button class="sm primary" onclick="drawerSevenR('${attr(c.app_id)}')">7R strategy</button><span class="muted" style="font-size:11px">scanned ${esc(c.scanned_at||'-')}</span></div>
    </div>`;
  }).join('');
  return `<div style="margin-top:8px"><div class="muted" style="font-size:12px;margin-bottom:2px">Code scan (executor feedback — migration-strategy reference)</div>${cards}</div>`;
}
// render the git sources bound to a host (via the Code tab) as chips, mirroring
// the Tags / Provenance rows. repo shape: {repo_id, url, branch, name}.
function _gitSourceHtml(repos){
  if(!repos || !repos.length) return '-';
  return repos.map(r=>{
    const label = esc(r.name || r.url);
    const branch = r.branch ? ` <span class="muted" style="font-size:11px">@${esc(r.branch)}</span>` : '';
    return `<span class="tag" title="${attr(r.url)}">${label}${branch}</span>`;
  }).join(' ');
}
function openServer(id){
  // fetch fresh full record (the row in state.last may be a partial list row)
  // in parallel, fetch the git sources bound to this host from the Code tab
  // (the /servers/{id} record carries AI-scan strategy, NOT the bound repos).
  Promise.all([
    fetch(API+'/servers/'+id).then(r=>r.json()),
    api('/hosts/'+encodeURIComponent(id)+'/repos').catch(()=>[]),   // best-effort: empty on error
  ]).then(([s, repos])=>{
    const m=s.match||{}, u=s.utilization||{};
    window._drawerServer = s;   // current record for setDisposition pre-select
    $('dTitle').textContent=s.hostname;
    const kv = (label, val) => `<div class="kv-row"><span class="kv-label">${label}</span><span class="kv-val">${val}</span></div>`;
    $('dBody').innerHTML=`
      <div class="muted">${esc(s.fqdn||'')} · ${esc(s.source_type||'')} · ${esc(s.role||'')}</div>
      <div class="kv">
        ${kv('OS', `${esc(s.os||'-')} ${esc(s.os_version||'')}`)}
        ${kv('CPU / RAM', `${s.cpu_cores||'-'} vCPU / ${s.mem_gb||'-'} GB`)}
        ${kv('Disks', _disksHtml(s.disks, s.utilization))}
        ${kv('Network', `${esc((s.ips||[]).join(', ')||'-')} · ${esc(s.subnet||'-')} · ${esc(s.vlan||'-')}`)}
        ${kv('Env / Crit', `${esc(s.env||'-')} <span class="pill ${esc(s.business_criticality||'')}">${esc(s.business_criticality||'-')}</span>`)}
        ${kv('Apps', esc((s.app_ids||[]).join(', ')||'-'))}
        ${kv('Git source', _gitSourceHtml(repos))}
        ${kv('Tags', (s.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join(' ')||'-')}
        ${kv('Utilization', `cpu ${u.cpu_p95??'-'}% · mem ${u.mem_p95??'-'}% · disk ${u.disk_used_pct??'-'}% <span class="muted">(${esc(u.source||'-')})</span>`)}
        ${kv('Provenance', (s.source_refs||[]).map(r=>`<span class="tag">${esc(r.source)}:${esc(r.source_id)}</span>`).join(' ')||'-')}
        ${kv('Coverage', _coverageBadge(s, m))}
        ${kv('Warranty', _warrantyBadge(s))}
        ${kv('OS support', _osEolBadge(s))}
        ${kv('7R policy', `${_stratBadge(s.seven_r)} <span class="muted">· ${esc(s.seven_r_source||'-')}</span>`)}
        ${kv('Disposition', _dispositionBadge(s))}
        ${kv('Wave(s)', ((s.waves||[]).length?(s.waves||[]).map(w=>`<span class="tag">${esc(w.name||w.id)}<span class="muted" style="font-size:10px"> · ${esc(w.stage||'-')}</span></span>`).join(' '):'<span class="muted">-</span>'))}
        ${kv('Target', `<b>${m.target?esc(m.target.product):'-'}</b> ${m.target?esc(m.target.spec):''} <span class="muted">@ ${m.target?esc(m.target.region):''}</span>`)}
        ${kv('Cost', _costBadge(s.cost))}
        ${kv('Rule', m.rationale?esc(m.rationale):'-')}
        ${kv('Alternatives', (m.alternatives&&m.alternatives.length)?m.alternatives.map(a=>`<span class="tag" style="color:var(--muted)">${esc(a)}</span>`).join(' '):'<span class="muted">-</span>')}
      </div>
      ${_codeSection(s.code)}
      <div class="row"><button class="primary" onclick="explainServer('${s.id}')">MigraQ explain match</button><button class="primary" onclick="drawerSevenRHost('${s.id}')">Analyze 7R</button><button onclick="rightSize('${s.id}')">right-size</button><button onclick="drawerAudit('${s.id}')">audit target</button><button onclick="setWarranty('${s.id}')">set warranty</button><button onclick="setDisposition('${s.id}')">set disposition</button></div>
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
  $('dExplain').innerHTML = `<span class="spinner"></span> asking the MigraQ for a 7R strategy for ${esc(appId)}…`;
  try{
    const r = await api('/strategy', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({app_id:appId})});
    if(!r.ok){ $('dExplain').innerHTML = `<span class="ev-err">7R failed: ${esc(r.error||'unknown')}</span>`; return; }
    const kc = (r.key_changes||[]).map(k=>`  • ${esc(k)}`).join('\n');
    $('dExplain').textContent =
      `${r.app_id} → ${r.strategy} (target ${r.target||'-'}, effort ${r.effort||'-'}, conf ${r.confidence!=null?r.confidence.toFixed(2):'-'})\n`+
      `${r.rationale||''}\n` + (kc?`key changes:\n${kc}`:'');
  }catch(e){ $('dExplain').innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; }
}
async function drawerSevenRHost(id){
  // per-host 7R analysis (sibling of the per-app drawerSevenR). Asks the MigraQ
  // what 7R fits THIS host, grounded in the host's full context, and shows
  // current-vs-recommended. Advisory — does not mutate; the hint tells the
  // operator which action button to use to act on the recommendation.
  $('dExplain').innerHTML = `<span class="spinner"></span> asking the MigraQ for a 7R strategy for this host…`;
  try{
    const r = await api('/strategy/host',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({server_id:id})});
    if(!r.ok){ $('dExplain').innerHTML = `<span class="ev-err">7R analysis failed: ${esc(r.error||'unknown')}</span>`; return; }
    const kc = (r.key_changes||[]).map(k=>`  • ${esc(k)}`).join('\n');
    const same = r.strategy === r.current_7r;
    const sameTxt = same ? '<span class="muted">(same as current — no change needed)</span>'
                         : '<span style="color:var(--amber)">(differs from current)</span>';
    const actHint = ['retain','retire'].includes(r.strategy)
      ? `use <b>set disposition</b> to keep this host ${esc(r.strategy)}.`
      : `use the per-app <b>7R strategy</b> button in the code scan card (a migrating R affects every host of the app).`;
    $('dExplain').innerHTML =
      `<div class="mcard" style="padding:10px">
        <div class="row" style="gap:14px;flex-wrap:wrap;align-items:baseline">
          <span><span class="muted">current 7R</span> ${_stratBadge(r.current_7r)}</span>
          <span><span class="muted">recommended</span> ${_stratBadge(r.strategy)}</span>
          ${sameTxt}
        </div>
        <div style="margin-top:6px">${_stratBadge(r.strategy)} → ${esc(r.target||'-')} <span class="muted">· effort ${esc(r.effort||'-')} · conf ${r.confidence!=null?r.confidence.toFixed(2):'-'}</span></div>
        <div class="muted" style="font-size:12px;margin-top:4px">${esc(r.rationale||'')}</div>
        ${kc?`<pre class="muted" style="font-size:11px;margin-top:4px;white-space:pre-wrap">key changes:\n${esc(kc)}</pre>`:''}
        <div class="muted" style="font-size:11px;margin-top:6px">Advisory. To act: ${actHint}</div>
      </div>`;
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

// ---------- run-cost (F2) ----------
function _money(n){ return '$'+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0}); }
function _costBadge(c){
  // c = {monthly,yearly,product,spec,region,basis,pricing_source} or null (no
  // pricebook / no match). Renders the host's cloud run-cost + sizing basis.
  if(!c) return '<span class="muted">-</span>';
  const zero = !(c.monthly||0) && !(c.yearly||0);
  const val = zero ? '<span class="muted">unpriced</span>'
    : `<b>${_money(c.yearly)}/yr</b> <span class="muted">· ${_money(c.monthly)}/mo</span>`;
  return `${val} <span class="muted">· ${esc(c.basis||'-')} · ${esc(c.pricing_source||'-')}</span>`;
}

// ---------- host disposition (retain / retire) ----------
const _DISP_COLOR = {'retain':'var(--green)', 'retire':'#e5484d'};
function _dispositionBadge(s){
  // s.disposition: 'retain' | 'retire' | '' (migrate). The host-level
  // disposition overrides the app-level 7R rule: a retain/retire host is pulled
  // out of every app wave into the trailing Retain / Retire waves on the next
  // Rebuild. Survives a Rebuild (separate legacy_dispositions table).
  const d = s.disposition || '';
  if(!d) return '<span class="muted">migrate</span>';
  return `<span class="tag" style="color:${_DISP_COLOR[d]||'var(--fg)'};font-weight:600">${d}</span> <span class="muted">· next Rebuild → ${d} wave</span>`;
}
function setDisposition(id){
  // operator per-host retain/retire override. Inline form (like setWarranty),
  // rendered into dExplain; Save PUTs + re-opens the drawer to refresh.
  const cur = (window._drawerServer && window._drawerServer.disposition) || '';
  $('dExplain').innerHTML = `
    <div class="mcard" style="padding:10px">
      <div style="font-weight:600;margin-bottom:8px">Host disposition</div>
      <div class="muted" style="font-size:12px;margin-bottom:8px">Override the app-level 7R rule for this host. <b>Retain</b> keeps it on-prem; <b>Retire</b> decommissions it; <b>Migrate</b> clears the override so it moves with its waves. Takes effect on the next Rebuild and survives it (stored by host identity, not the rebuild-changing server row).</div>
      <div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
        <label style="font-size:13px">disposition
          <select id="dispSel" style="margin-left:4px">
            <option value="">(migrate — no override)</option>
            <option value="retain">retain (keep on-prem)</option>
            <option value="retire">retire (decommission)</option>
          </select>
        </label>
        <button class="sm primary" onclick="saveDisposition('${id}')">Save</button>
        <button class="sm" onclick="document.getElementById('dExplain').innerHTML=''">Cancel</button>
      </div>
    </div>`;
  const sel = $('dispSel');
  if(cur) sel.value = cur;
}
async function saveDisposition(id){
  const disp = $('dispSel').value;
  try{
    const r = await api('/servers/'+id+'/disposition', {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify({disposition:disp})});
    toast(`disposition ${r.cleared?'cleared':'set'}: ${r.disposition||'(migrate)'}`, 'ok');
    $('dExplain').innerHTML='';
    openServer(id);  // re-open to refresh the badge
  }catch(e){ toast('set disposition failed: '+e, 'err'); }
}
