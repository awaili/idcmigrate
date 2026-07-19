/* ---------- Scan & Migrate — workload-centric assessment ----------
   One unit = a workload (an app + its bound git sources + servers + DBs). Pick
   an app once; everything below is scoped to it. The page answers, in order:
     ① what will BREAK in the cloud (migration blockers)
     ② what is already ROTTEN / risky (EOL · expired · vulnerable)
     ③ what the MIGRATION IMPACT is (calls / deps / DBs / consumers)
     ④ what to DO (7R · DB conversion · containerize · EOL disposition · IaC)
   Findings are grouped by question, not flattened into a 9-column table — a
   migration expert reads by question, not by column. */

// finding-category taxonomy — the expert's four questions, mapped to the real
// categories the executor emits (idc/core/models.py FINDING_CATEGORIES).
const _BREAK_CATS = new Set(["hardcoded_ip","service_discovery","db_connection",
  "secrets_in_repo","stateful_local","os_dependency","baremetal_assumption",
  "scheduled_job","config_coupling","network_dependency"]);
const _ROT_CATS   = new Set(["legacy_runtime","library_eol","expired_cert","weak_crypto"]);
// gap categories the executor does NOT detect yet — shown as honest empty-states
// so the operator knows to flag them in the migration plan until the scanner
// catches up (library EOL/CVE, expired certs, weak crypto — the user's ask).
const _ROT_GAPS = [
  {cat:"library_eol",  label:"EOL / CVE-laden libraries (pom.xml · requirements.txt · package.json · go.mod)"},
  {cat:"expired_cert", label:"expired / expiring TLS certificates committed in-repo"},
  {cat:"weak_crypto",  label:"weak / deprecated crypto (MD5 · SHA1 · DES · RC4 · SSLv3)"},
];
const _SEV_COLOR = {blocker:'var(--red)', high:'#e5484d', medium:'var(--amber)', low:'#888'};
const _SEV_ICON  = {blocker:'🔴', high:'🟠', medium:'🟡', low:'⚪'};
const _CAT_LABEL = {
  hardcoded_ip:"hardcoded IP", service_discovery:"service-discovery endpoint",
  db_connection:"DB connection string", secrets_in_repo:"secret in repo",
  stateful_local:"local stateful write", os_dependency:"OS / shell call",
  baremetal_assumption:"bare-metal assumption", scheduled_job:"scheduled job / cron",
  config_coupling:"absolute config path", network_dependency:"network dependency",
  legacy_runtime:"EOL runtime / language", library_eol:"EOL / vulnerable library",
  expired_cert:"expired certificate", weak_crypto:"weak crypto",
};
// data-tier cloud products → the workload's DB hosts (for DB conversion + impact).
const _DB_PRODUCT_RE = /CDB|TData|TDSQL|Postgres|Redis|Mongo|MariaDB|TencentDB/i;
function _isDbProduct(p){ return _DB_PRODUCT_RE.test(p||''); }

let _wl = null;       // current workload context (see selectWorkload)
let _wlQTimer = null; // blocked-questions poller, scoped to the loaded workload

async function loadCode(){
  loadExecStatus();        // default-executor connected badge (intro card)
  populateExecPickers();   // fill the Advanced executor <select>
  populateAppPickers();    // #appList datalist for the workload picker
  _wl = null;
  if(_wlQTimer){ clearInterval(_wlQTimer); _wlQTimer = null; }
  // reset to the empty state — no workload loaded yet
  $('wlScope')?.classList.add('hidden');
  $('wlCtx').textContent = '';
  $('wlSources').innerHTML = '';
  $('wlScanOut').innerHTML = '';
  $('wlJobStatus').textContent = '';
  $('wlQuestions').innerHTML = '<span class="muted">No pending questions. The executor raises one here when it can\'t resolve a change (e.g. an unknown hardcoded IP).</span>';
  $('wlProfileHdr').textContent = '';
  $('wlFindings').innerHTML = '<span class="muted">Pick a workload above and hit <b>Scan workload</b> to see what blocks migration, what is EOL / expired / vulnerable, and the migration impact.</span>';
  $('wlDecisions').innerHTML = '<span class="muted">Load a workload to assign a 7R strategy, assess its DBs, analyze EOL hosts, infer a container scaffold, or emit IaC + guardrails.</span>';
  $('wlDbConvDetail')?.classList.add('hidden');
  $('wlIacDetail')?.classList.add('hidden');
  // Enter / change on the picker loads the workload (idempotent wire)
  const inp = $('wlApp'); if(inp && !inp._wlWired){ inp._wlWired = true; inp.addEventListener('change', selectWorkload); }
}

// Fetch the full workload context in parallel. There is no aggregated endpoint,
// so we fan out the reads that define a workload; each is best-effort (a 404 for
// "no profile yet" / "no strategy yet" is a normal empty state, not an error).
async function _wlFetch(app){
  const g = (k, p) => p.then(v=>[k, v]).catch(e=>[k, {__err: String(e)}]);
  const entries = await Promise.all([
    g('sources',  api('/apps/'+encodeURIComponent(app)+'/sources')),
    g('targets',  api('/apps/'+encodeURIComponent(app)+'/targets')),
    g('profile',  api('/code-profiles/'+encodeURIComponent(app))),
    g('strategy', api('/strategies/'+encodeURIComponent(app))),
    g('dbProfiles', api('/db-profiles')),
    g('allProfiles', api('/code-profiles')),
    g('legacy',   api('/legacy-dispositions')),
    g('iac',      api('/iac-artifacts')),
    g('jobs',     api('/change-jobs')),
    g('questions',api('/apps/'+encodeURIComponent(app)+'/questions?status=pending')),
  ]);
  return Object.fromEntries(entries);
}
async function selectWorkload(){
  const app = ($('wlApp').value||'').trim();
  if(!app){ toast('enter an app_id','warn'); return; }
  $('wlCtx').innerHTML = '<span class="spinner"></span> loading workload…';
  _wl = { app, ...(await _wlFetch(app)) };
  // blocked-questions poller, scoped to this workload
  if(_wlQTimer) clearInterval(_wlQTimer);
  _wlQTimer = setInterval(()=>{ if(_wl) loadWorkloadQuestions(); }, 10000);
  renderWorkload();
}
// re-fetch the volatile parts after an action (scan / 7R / db-scan / legacy / iac)
async function refreshWorkload(){
  if(!_wl) return;
  Object.assign(_wl, await _wlFetch(_wl.app));
  renderWorkload();
}

function renderWorkload(){
  if(!_wl) return;
  const app = _wl.app;
  const srcs = (_wl.sources && !_wl.sources.__err && _wl.sources.sources) || [];
  const targets = (_wl.targets && !_wl.targets.__err && _wl.targets.targets) || [];
  const dbHosts = targets.filter(t=>_isDbProduct(t.product));
  $('wlScope')?.classList.remove('hidden');
  $('wlCtx').innerHTML = `<b>${esc(app)}</b> · ${targets.length} server(s) · ${srcs.length} git source(s) · ${dbHosts.length} DB host(s)`
    + (_wl.strategy && !_wl.strategy.__err && _wl.strategy.strategy ? ` · 7R ${_strategyBadge(_wl.strategy.strategy)}` : '');
  $('wlSources').innerHTML = srcs.length
    ? `<span class="muted" style="font-size:12px">git sources (bound on the Code tab):</span> ` + srcs.map(s=>`<span class="tag" title="${attr(s.url)}">${esc(s.name||s.url)}${s.branch?' · '+esc(s.branch):''}</span>`).join(' ')
    : '<span class="muted" style="font-size:12px">no git sources bound — bind one on the <b>Code</b> tab, or use <b>Containerize scaffold</b> below for a no-source workload.</span>';
  renderScanStatus();
  renderFindings();
  renderDecisions();
  loadWorkloadQuestions();
}

function renderScanStatus(){
  const el = $('wlJobStatus'); if(!el) return;
  const jobs = (_wl.jobs && Array.isArray(_wl.jobs)) ? _wl.jobs.filter(j=>j.app_id===_wl.app) : [];
  if(!jobs.length){ el.textContent = ''; return; }
  const j = jobs.slice().sort((a,b)=>(b.created_at||'').localeCompare(a.created_at||''))[0];
  const col = {done:'var(--green)', error:'var(--red)', timeout:'var(--red)', running:'var(--amber)'}[j.status] || 'var(--fg)';
  el.innerHTML = `last job: <span style="color:${col}">${esc(j.kind)} ${esc(j.status)}</span> · ${esc(_ago(j.created_at)||'-')}`;
}

/* ----- Card C — findings (the hero) ----- */
function _sevRank(s){ return {blocker:0, high:1, medium:2, low:3}[s] ?? 4; }
function renderFindings(){
  const wrap = $('wlFindings'); const hdr = $('wlProfileHdr'); if(!wrap) return;
  const p = _wl.profile;
  if(!p || p.__err){
    hdr.textContent = '';
    wrap.innerHTML = '<span class="muted">No code profile yet — hit <b>Scan workload</b> to scan the bound git sources.</span>';
    return;
  }
  const stack = [p.language, p.runtime, p.framework].filter(Boolean).join(' / ');
  const _afN = (p.agent_findings||[]).length;
  hdr.innerHTML = `${esc(stack||'?')} · readiness ${p.cloud_readiness!=null?(p.cloud_readiness*100).toFixed(0):'?'}% · 7R ${_strategyBadge(p.migration_pattern)} · effort ${esc(p.refactor_effort||'-')} · ${(p.blockers||[]).length} blocker(s) · ${(p.findings||[]).length} finding(s)` + (_afN?` · ${_afN} agent finding(s)`:'') + ` · scanned ${esc(_ago(p.scanned_at)||'-')}`;
  const findings = p.findings || [];
  const bre = findings.filter(f=>_BREAK_CATS.has(f.category));
  const rot = findings.filter(f=>_ROT_CATS.has(f.category));
  wrap.innerHTML =
    _renderFindingGroup('① Will break in the cloud', 'must-fix migration blockers — these tie the code to this on-prem estate', bre)
    + _renderRotGroup(rot, p)
    + _renderImpact(p)
    + _renderAgent(p);
}
function _renderAgent(p){
  // path A — the optional codex pass output (agent_findings/agent_blockers/
  // agent_summary), read from the actual repo by the executor. Surfaces what the
  // agent found so the operator can verify it (not just have it silently feed the
  // LLM prompts). Empty/absent when the pass is off / produced nothing.
  const af = p.agent_findings || [];
  const ab = p.agent_blockers || [];
  const asum = (p.agent_summary || '').trim();
  if(!af.length && !ab.length && !asum) return '';
  const blockersLine = ab.length
    ? `<div style="color:var(--amber);margin:6px 0;font-size:13px">⚠ agent blockers (read from the actual repo — weight HIGHER than rule blockers): ${esc(ab.join('; '))}</div>`
    : '';
  const summaryLine = asum ? `<div class="muted" style="font-size:12px;margin:6px 0">${esc(asum)}</div>` : '';
  const grp = af.length
    ? _renderFindingGroup('🤖 Agent (codex) findings', 'code-level issues read from the actual repo by the optional codex pass — these catch semantic / cross-file issues the rule engine misses', af)
    : '';
  return `<div style="margin-top:12px;border-top:1px dashed var(--border);padding-top:8px">
    <div style="font-weight:600;font-size:13px">🤖 Agent (codex) pass <span class="muted" style="font-size:12px">— grounded in the actual repo (executor EXECUTOR_CODEX_SCAN)</span></div>
    ${summaryLine}${blockersLine}${grp}
  </div>`;
}
function _renderFindingGroup(title, sub, list){
  const counts = {blocker:0, high:0, medium:0, low:0};
  list.forEach(f=>{ const s=f.severity||'medium'; counts[s]=(counts[s]||0)+1; });
  const badges = Object.entries(counts).filter(([k,v])=>v).map(([k,v])=>`<span style="color:${_SEV_COLOR[k]}">${_SEV_ICON[k]}${v}</span>`).join(' ');
  const body = list.length
    ? list.slice().sort((a,b)=>_sevRank(a.severity)-_sevRank(b.severity)).map(_renderFindingRow).join('')
    : '<div class="muted" style="font-size:12px;padding:6px 0">none found — good.</div>';
  return `<details open style="margin-top:10px">
    <summary style="cursor:pointer;font-weight:600">${title} <span class="muted" style="font-size:12px">(${list.length})</span> ${badges}</summary>
    <div class="muted" style="font-size:12px;margin:2px 0 6px">${sub}</div>${body}
  </details>`;
}
function _renderFindingRow(f){
  const sev = f.severity || 'medium';
  const loc = f.file ? `${esc(f.file)}${f.line?':'+esc(f.line):''}` : '<span class="muted">(no location)</span>';
  const ev = f.evidence ? `<code style="font-size:11px;word-break:break-all">${esc(f.evidence)}</code>` : '';
  const rem = f.remediation ? `<div class="muted" style="font-size:12px">→ ${esc(f.remediation)}</div>` : '';
  return `<div class="mcard" style="margin:4px 0;padding:6px 8px">
    <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
      <span style="color:${_SEV_COLOR[sev]}" title="${attr(sev)}">${_SEV_ICON[sev]}</span>
      <b style="font-size:13px">${esc(_CAT_LABEL[f.category]||f.category)}</b>
      <span class="muted" style="font-size:11px">${loc}</span>
    </div>
    ${f.message?`<div style="font-size:13px;margin:2px 0">${esc(f.message)}</div>`:''}${ev}${rem}
  </div>`;
}
function _renderRotGroup(rot, p){
  const found = rot.length ? rot.slice().sort((a,b)=>_sevRank(a.severity)-_sevRank(b.severity)).map(_renderFindingRow).join('') : '';
  const gapRows = _ROT_GAPS.map(g=>{
    if(rot.some(f=>f.category===g.cat)) return '';   // already covered by a real finding
    return `<div class="mcard" style="margin:4px 0;padding:6px 8px;opacity:.85">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <span class="muted">⚪</span><b style="font-size:13px">${esc(g.label)}</b>
        <span class="tag" style="color:var(--amber);border-color:var(--amber)">not yet scanned</span>
      </div>
      <div class="muted" style="font-size:12px;margin-top:2px">backend detector pending — flag this in the migration plan until the scanner covers it.</div>
    </div>`;
  }).join('');
  const rtNote = p.runtime ? `<div class="muted" style="font-size:12px;margin:2px 0 4px">detected runtime: <b>${esc(p.runtime)}</b> — confirm EOL status against your estate before cutover.</div>` : '';
  return `<details open style="margin-top:10px">
    <summary style="cursor:pointer;font-weight:600">② Rotten / risky <span class="muted" style="font-size:12px">(EOL · expired · vulnerable)</span> <span class="muted" style="font-size:12px">(${rot.length})</span></summary>
    <div class="muted" style="font-size:12px;margin:2px 0 6px">things already past EOL / expiring / vulnerable — migration is the forcing function to fix them.</div>
    ${rtNote}${found}${gapRows}
  </details>`;
}
function _renderImpact(p){
  const eps = p.network_endpoints || [];
  const deps = p.code_deps || [];
  // downstream consumers: other apps whose code_deps include this workload
  const all = (_wl.allProfiles && Array.isArray(_wl.allProfiles)) ? _wl.allProfiles : [];
  const consumers = all.filter(q=> q.app_id!==_wl.app && (q.code_deps||[]).includes(_wl.app)).map(q=>q.app_id);
  const targets = (_wl.targets && !_wl.targets.__err && _wl.targets.targets) || [];
  const dbHosts = targets.filter(t=>_isDbProduct(t.product)).map(t=>t.server_id);
  const dbp = (_wl.dbProfiles && Array.isArray(_wl.dbProfiles)) ? _wl.dbProfiles : [];
  const dbRows = dbHosts.length ? dbHosts.map(h=>{
    const d = dbp.find(x=>x.db_server_id===h);
    if(!d) return `<div class="mcard" style="margin:3px 0;padding:4px 8px"><b>${esc(h)}</b> <span class="muted">· not assessed yet</span></div>`;
    return `<div class="mcard" style="margin:3px 0;padding:4px 8px"><b>${esc(h)}</b> <span class="muted">${esc(d.source_engine||'?')}→${esc(d.target_engine||'?')}</span> · grade <span style="color:${_gradeColor(d.difficulty)};font-weight:600">${esc(d.difficulty||'-')}</span> <span class="muted">${d.est_man_days!=null?esc(d.est_man_days)+'d':''} · ${(d.blockers||[]).length} blocker(s)</span></div>`;
  }).join('') : '<div class="muted" style="font-size:12px">no DB hosts in this workload.</div>';
  const chip = arr => arr.length ? arr.map(x=>`<span class="tag">${esc(x)}</span>`).join(' ') : '<span class="muted">—</span>';
  return `<details style="margin-top:10px">
    <summary style="cursor:pointer;font-weight:600">③ Migration impact <span class="muted" style="font-size:12px">(what it touches / what touches it → drives waves + cutover)</span></summary>
    <div style="margin-top:6px;font-size:13px">
      <div class="muted" style="font-size:12px">calls (network endpoints):</div><div style="margin:2px 0 8px">${chip(eps)}</div>
      <div class="muted" style="font-size:12px">depends on (apps):</div><div style="margin:2px 0 8px">${chip(deps)}</div>
      <div class="muted" style="font-size:12px">consumed by (apps that depend on this one):</div><div style="margin:2px 0 8px">${chip(consumers)}</div>
      <div class="muted" style="font-size:12px">DBs used:</div><div style="margin:2px 0 4px">${dbRows}</div>
    </div>
  </details>`;
}

/* ----- Card D — decisions & actions (scoped to this workload) ----- */
function _renderStoredStrategy(st){
  if(!st || !st.strategy) return '<span class="muted">not assigned yet.</span>';
  const kc = (st.key_changes||[]).map(k=>`<li>${esc(k)}</li>`).join('');
  return `<div class="row" style="gap:14px;align-items:baseline;flex-wrap:wrap">
      <div><span class="muted">strategy</span> ${_strategyBadge(st.strategy)}</div>
      <div><span class="muted">target</span> <b>${esc(st.target||'-')}</b></div>
      <div><span class="muted">effort</span> ${esc(st.effort||'-')}</div>
      <div><span class="muted">confidence</span> ${(st.confidence!=null?Number(st.confidence).toFixed(2):'-')}</div>
      <div><span class="muted">source</span> ${esc(st.source||'-')}</div>
    </div>
    <div style="margin:4px 0">${esc(st.rationale||'')}</div>
    ${kc?`<div class="muted">key changes:</div><ul style="margin:2px 0 4px 18px">${kc}</ul>`:''}`;
}
function renderDecisions(){
  const wrap = $('wlDecisions'); if(!wrap) return;
  const st = _wl.strategy;
  const targets = (_wl.targets && !_wl.targets.__err && _wl.targets.targets) || [];
  const srcs = (_wl.sources && !_wl.sources.__err && _wl.sources.sources) || [];
  const strat = `<details open style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
      <summary style="cursor:pointer;font-weight:600">7R strategy</summary>
      <div style="margin:4px 0">${st && !st.__err ? _renderStoredStrategy(st) : '<span class="muted">not assigned yet.</span>'}</div>
      <div class="row" style="gap:8px;align-items:center;margin-top:4px">
        <label class="muted" style="white-space:nowrap;font-size:12px"><input type="checkbox" id="wlStratApply" checked> apply to app_strategies</label>
        <button class="sm primary" onclick="assignWorkloadStrategy()">Assign 7R</button>
      </div>
      <div class="out" id="wlStratOut" style="margin-top:4px"></div>
    </details>`;
  wrap.innerHTML = strat + _renderDecisionDb(targets) + (srcs.length ? '' : _renderDecisionContainerize(targets)) + _renderDecisionServers(targets);
}
function _renderDecisionDb(targets){
  const dbHosts = targets.filter(t=>_isDbProduct(t.product));
  if(!dbHosts.length) return '';
  const dbp = (_wl.dbProfiles && Array.isArray(_wl.dbProfiles)) ? _wl.dbProfiles : [];
  const rows = dbHosts.map(t=>{
    const h = t.server_id;
    const d = dbp.find(x=>x.db_server_id===h);
    const grade = d ? ` · grade <span style="color:${_gradeColor(d.difficulty)};font-weight:600">${esc(d.difficulty||'-')}</span> <span class="muted">${d.est_man_days!=null?esc(d.est_man_days)+'d':''}</span>` : ' · <span class="muted">not assessed</span>';
    const convBtn = (d && d.conversion && (d.conversion.objects||[]).length) ? `<button class="sm" onclick="showDbConv('${attr(h)}')">report</button>` : '';
    return `<tr><td><b>${esc(h)}</b></td>
      <td class="muted" style="font-size:11px">${esc(t.product||'')} ${esc(t.spec||'')}</td>
      <td>${grade}</td>
      <td><button class="sm" onclick="assessDb('${attr(h)}')">Assess</button> <button class="sm primary" onclick="convertDb('${attr(h)}')">Convert</button> ${convBtn}</td></tr>`;
  }).join('');
  return `<details open style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
    <summary style="cursor:pointer;font-weight:600">DB conversion <span class="muted" style="font-size:12px">(${dbHosts.length} DB host(s))</span></summary>
    <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0">
      <span class="muted" style="font-size:12px">source → target:</span>
      <select id="wlDbSrc" style="flex:0 1 auto"><option value="oracle">oracle</option><option value="sqlserver">sqlserver</option><option value="mysql">mysql</option></select>
      <span class="muted">→</span>
      <select id="wlDbTgt" style="flex:0 1 auto"><option value="tdsql">tdsql</option><option value="cdb_mysql">cdb_mysql</option><option value="postgresql">postgresql</option></select>
    </div>
    <div class="xscroll"><table class="tbl mcard"><thead><tr><th>host</th><th>target</th><th>grade</th><th>actions</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="out" id="wlDbOut" style="margin-top:4px"></div>
  </details>`;
}
function _renderDecisionContainerize(targets){
  const opts = targets.map(t=>`<option value="${attr(t.server_id)}">${esc(t.server_id)} — ${esc(t.product||'')} ${esc(t.spec||'')}</option>`).join('');
  return `<details open style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
    <summary style="cursor:pointer;font-weight:600">Containerize scaffold <span class="tag" style="color:var(--amber);border-color:var(--amber)">no source repo</span></summary>
    <div class="muted" style="font-size:12px;margin:4px 0">no git source bound — infer a Dockerfile from runtime telemetry (Zabbix/Prometheus). Confidence capped at 0.5.</div>
    <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center">
      <select id="wlRtServer" style="flex:1 1 220px;min-width:160px"><option value="">— pick a server —</option>${opts}</select>
      <input id="wlRtProc" placeholder="process (e.g. java -jar app.jar)" style="flex:1 1 200px;min-width:140px"/>
      <input id="wlRtPort" placeholder="port" style="flex:0 1 80px;min-width:60px"/>
      <input id="wlRtSoft" placeholder="software (e.g. openjdk-8)" style="flex:0 1 140px;min-width:100px"/>
      <select id="wlRtMode" style="flex:0 1 auto"><option value="plan">plan</option><option value="execute">execute</option></select>
      <button class="sm" onclick="prefillRuntimeInventory()">Pre-fill</button>
      <button class="sm primary" onclick="inferScaffold()">Infer scaffold</button>
    </div>
    <div class="out" id="wlRtOut" style="margin-top:4px"></div>
  </details>`;
}
function _renderDecisionServers(targets){
  if(!targets.length) return '';
  const ds = (_wl.legacy && Array.isArray(_wl.legacy)) ? _wl.legacy : [];
  const iac = (_wl.iac && Array.isArray(_wl.iac)) ? _wl.iac : [];
  const iacMap = {}; iac.forEach(a=>{ iacMap[a.scope_id] = a; });
  const CAP = 50;
  const rows = targets.slice(0, CAP).map(t=>{
    const sid = t.server_id;
    const d = ds.find(x=>x.server_id===sid);
    const disp = d ? `<span style="color:${_LD_COLOR[d.disposition]||'var(--fg)'};font-weight:600">${esc(d.disposition)}</span>` : '<span class="muted">—</span>';
    const a = iacMap['wl:'+sid];
    const gr = a ? (a.guardrail_pass ? '<span style="color:var(--green)">✓ pass</span>' : '<span style="color:var(--red)">✗ fail</span>') : '<span class="muted">—</span>';
    const iacBtn = a ? `<button class="sm" onclick="showIac('wl:${attr(sid)}')">show</button>` : '';
    return `<tr><td><b>${esc(sid)}</b></td>
      <td class="muted" style="font-size:11px">${esc(t.product||'')} ${esc(t.spec||'')}</td>
      <td>${disp}</td><td>${gr} ${iacBtn}</td>
      <td><button class="sm" onclick="analyzeLegacy('${attr(sid)}')">Analyze EOL</button> <button class="sm" onclick="emitIac('${attr(sid)}')">Emit IaC</button></td></tr>`;
  }).join('');
  const more = targets.length > CAP ? `<div class="muted" style="font-size:12px;margin-top:4px">showing ${CAP} of ${targets.length} servers.</div>` : '';
  return `<details style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
    <summary style="cursor:pointer;font-weight:600">Servers <span class="muted" style="font-size:12px">(${targets.length} · EOL disposition + IaC guardrails)</span></summary>
    <div class="xscroll" style="margin-top:6px"><table class="tbl mcard"><thead><tr><th>server</th><th>target</th><th>disposition</th><th>guardrails</th><th>actions</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${more}
  </details>`;
}
// recent /change-jobs — executor pushes results back here. Lives on the Executor
// management page (#jobTbl), so guard against the table being absent elsewhere.
async function loadExecJobs(){
  const jb = $('jobTbl'); if(!jb) return; const tb = jb.querySelector('tbody');
  try{
    const jobs = await api('/change-jobs');
    tb.innerHTML='';
    jobs.forEach(j=> tb.insertAdjacentHTML('beforeend', `<tr>
      <td data-label="id">${esc(j.id)}</td><td data-label="app_id">${esc(j.app_id)}</td><td data-label="kind">${esc(j.kind)}</td>
      <td data-label="status">${esc(j.status)}</td><td data-label="summary" class="conf" title="${esc(j.summary||'')}">${esc((j.summary||'-').slice(0,60))}</td>
      <td data-label="patch_ref">${esc(j.patch_ref||'-')}</td><td data-label="created">${esc(j.created_at||'-')}</td></tr>`));
    if(!jobs.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="7" class="muted">no executor jobs yet.</td></tr>`);
  }catch(e){ tb.innerHTML = `<tr><td colspan="7" class="ev-err">${esc(e)}</td></tr>`; }
}
/* ----- Workload actions (Card A trigger + Card D decisions) ----- */

/* Scan workload: enqueue one executor task per bound git source (the executor
   scans one repo at a time). Polls the mirrored change-jobs until every task
   reaches a terminal state, then re-fetches the profile so findings refresh.
   action/mode/scope/overrides come from the Advanced disclosure; the default
   button is a plain scan in plan mode. */
async function scanWorkload(){
  if(!_wl){ toast('load a workload first','warn'); return; }
  const app = _wl.app;
  const srcs = (_wl.sources && !_wl.sources.__err && _wl.sources.sources) || [];
  const out = $('wlScanOut');
  if(!srcs.length){ out.innerHTML = '<span class="ev-err">no git sources bound to this app — bind one on the Code tab, or use <b>Containerize scaffold</b> below for a no-source workload.</span>'; return; }
  const action = $('wlAction').value;
  const mode = $('wlMode').value;
  const execId = ($('wlExecutor').value||'').trim();
  let scope = null, overrides = null;
  if(action === 'modify'){
    const s = $('wlScopeCats').value.trim();
    if(s) scope = s.split(',').map(x=>x.trim()).filter(Boolean);
    overrides = parseOverrides($('wlOverrides').value);
  }
  out.innerHTML = `<span class="spinner"></span> enqueuing ${esc(action)} for ${srcs.length} source(s)…`;
  const tasks = [];
  for(const s of srcs){
    const body = {app_id: app, repo_url: s.url, branch: (s.branch||''), action, mode, executor_id: execId};
    if(scope) body.scope = scope;
    if(overrides && Object.keys(overrides).length) body.overrides = overrides;
    try{
      const r = await fetch(API+'/executor/trigger', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
      const txt = await r.text();
      if(!r.ok) throw new Error(txt);
      const j = JSON.parse(txt);
      tasks.push({src: s.name||s.url, job_id: j.job_id||j.task_id, ok: true, changes: j.changes});
    }catch(e){ tasks.push({src: s.name||s.url, ok: false, err: String(e)}); }
  }
  const okIds = tasks.filter(t=>t.ok).map(t=>t.job_id);
  const fails = tasks.filter(t=>!t.ok);
  const chgLines = tasks.filter(t=>t.ok && t.changes && t.changes.length)
    .map(t=>`<div class="muted">${esc(t.src)}: ${t.changes.length} concrete change(s)</div>`).join('');
  out.innerHTML = `<div class="muted">enqueued ${okIds.length}/${srcs.length} ${esc(action)} task(s). ${fails.length?`<span class="ev-err">${fails.length} failed: ${esc(fails.map(f=>f.src+': '+f.err).join('; '))}</span>`:''} Executor runs async and pushes results back → findings refresh automatically.</div>${chgLines}`;
  if(okIds.length) _pollWorkloadScan(okIds);
}
async function _pollWorkloadScan(jobIds){
  const wanted = new Set(jobIds);
  let attempts = 0;
  const tick = async () => {
    attempts++;
    let jobs = []; try{ jobs = await api('/change-jobs'); }catch(e){ jobs = []; }
    const mine = jobs.filter(j=>wanted.has(j.id));
    const done = mine.filter(j=>['done','error','timeout'].includes(j.status));
    $('wlJobStatus').innerHTML = `scanning… ${done.length}/${mine.length} done`;
    if(mine.length && done.length === mine.length){ await refreshWorkload(); loadExecJobs(); return; }
    if(attempts < 80) setTimeout(tick, 1500);   // ~2 min before we stop watching
    else { $('wlJobStatus').innerHTML = '<span class="ev-err">scan still running — refresh later</span>'; await refreshWorkload(); }
  };
  setTimeout(tick, 1500);
}

/* show the Advanced scope/overrides fields only for modify */
function toggleModifyFields(){
  $('wlModifyFields').classList.toggle('hidden', $('wlAction').value !== 'modify');
}

async function assignWorkloadStrategy(){
  if(!_wl){ toast('load a workload first','warn'); return; }
  const app_id = _wl.app;
  const apply = $('wlStratApply') && $('wlStratApply').checked;
  const out = $('wlStratOut'); if(!out) return;
  out.innerHTML = `<span class="spinner"></span> asking the MigraQ for a 7R strategy for ${esc(app_id)}…`;
  let r; try{
    r = await api('/strategy', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({app_id, apply})});
  }catch(e){ out.innerHTML = '<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  out.innerHTML = _renderStrategy(r, apply);
  if(apply) refreshWorkload();   // persist the assigned strategy into the stored view
}

async function _doDbScan(hostId, mode){
  if(!_wl) return;
  const body = {db_server_id: hostId, source_engine: $('wlDbSrc').value, target_engine: $('wlDbTgt').value, mode, executor_id: ''};
  const out = $('wlDbOut');
  try{
    await api('/db-scan', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    if(out) out.innerHTML = `<span class="muted">DB ${esc(mode)} requested for ${esc(hostId)} — grade arrives via callback.</span>`;
    toast(`DB ${mode} requested for ${hostId}`, 'ok');
  }catch(e){ if(out) out.innerHTML = `<span class="ev-err">DB scan failed: ${esc(e)}</span>`; toast('DB scan failed: '+e, 'err'); }
  setTimeout(()=>{ refreshWorkload(); loadExecJobs(); }, 1500);
}
function assessDb(h){ return _doDbScan(h, 'assess'); }
function convertDb(h){ return _doDbScan(h, 'convert'); }

async function analyzeLegacy(serverId){
  if(!_wl) return;
  // best-effort context: the executor fills the gaps (role/os/runtime from the
  // host); we pass what we know — whether the app has source, and an EOL signal.
  const ctx = {has_source_repo: ((_wl.sources && !_wl.sources.__err && _wl.sources.sources)||[]).length > 0, os_eol_bucket: 'expired'};
  try{
    await api('/legacy-disposition', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({server_id: serverId, context: ctx, executor_id: ''})});
    toast(`EOL analysis requested for ${serverId}`, 'ok');
  }catch(e){ toast('legacy-disposition failed: '+e, 'err'); }
  setTimeout(()=>{ refreshWorkload(); loadExecJobs(); }, 1500);
}

async function emitIac(serverId){
  try{
    await api('/iac-emit', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({scope:'workload', scope_id:'wl:'+serverId, context:{}, executor_id:''})});
    toast(`IaC emit requested for ${serverId}`, 'ok');
  }catch(e){ toast('iac-emit failed: '+e, 'err'); }
  setTimeout(()=>{ refreshWorkload(); loadExecJobs(); }, 1500);
}

/* no-source path: infer a Dockerfile scaffold from runtime telemetry */
async function prefillRuntimeInventory(){
  const sid = $('wlRtServer').value.trim();
  if(!sid){ toast('pick a server first','warn'); return; }
  try{
    const inv = await api('/runtime-inventory/'+encodeURIComponent(sid));
    $('wlRtProc').value = inv.process || '';
    $('wlRtPort').value = (inv.ports||[]).join(',');
    $('wlRtSoft').value = (inv.software||[]).join(',');
    toast('pre-filled from server telemetry', 'ok');
  }catch(e){ toast('pre-fill failed: '+e, 'err'); }
}
async function inferScaffold(){
  if(!_wl){ return; }
  const server_id = $('wlRtServer').value.trim();
  if(!server_id){ toast('pick a server first','warn'); return; }
  const inv = {};
  if($('wlRtProc').value.trim()) inv.process = $('wlRtProc').value.trim();
  if($('wlRtPort').value.trim()) inv.ports = $('wlRtPort').value.split(',').map(s=>parseInt(s.trim())).filter(n=>n);
  if($('wlRtSoft').value.trim()) inv.software = $('wlRtSoft').value.split(',').map(s=>s.trim()).filter(s=>s);
  const body = {app_id: _wl.app, server_id, inventory: inv, mode: $('wlRtMode').value, executor_id: ''};
  const out = $('wlRtOut');
  try{
    await api('/runtime-containerize', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    if(out) out.innerHTML = '<span class="muted">scaffold requested — a runtime-derived CodeProfile arrives via callback.</span>';
    toast('containerize requested', 'ok');
  }catch(e){ if(out) out.innerHTML = `<span class="ev-err">${esc(e)}</span>`; toast('runtime-containerize failed: '+e, 'err'); }
  setTimeout(()=>{ refreshWorkload(); loadExecJobs(); }, 1500);
}

/* blocked-questions queue, scoped to the loaded workload (with a one-line note
   for pending questions on OTHER workloads, so nothing is silently hidden). */
async function loadWorkloadQuestions(){
  const el = $('wlQuestions'); if(!el) return;
  let all = []; try{ all = await api('/questions?status=pending'); }catch(e){ el.innerHTML = '<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  const app = _wl && _wl.app;
  const mine = app ? all.filter(q=>q.app_id===app) : [];
  const others = app ? all.filter(q=>q.app_id!==app) : all;
  if(!mine.length && !others.length){ el.innerHTML = '<span class="muted">No pending questions. The executor raises one here when it can\'t resolve a change (e.g. an unknown hardcoded IP).</span>'; return; }
  el.innerHTML = mine.map(_renderQuestion).join('')
    + (others.length ? `<div class="muted" style="font-size:12px;margin-top:6px">+ ${others.length} pending for other workloads (load that workload to answer them).</div>` : '');
}
function _renderQuestion(q){
  const ctx = q.context || {};
  const loc = ctx.file ? `${esc(ctx.file)}${ctx.line?':'+ctx.line:''}` : '(location unknown)';
  const oldNew = [];
  if(ctx.old) oldNew.push(`old: <code>${esc(ctx.old)}</code>`);
  if(ctx.new) oldNew.push(`new: <code>${esc(ctx.new)}</code>`);
  const opts = (q.options||[]).map(o=>`<button class="sm" onclick="answerQuestion('${q.id}','${attr(o)}')">${esc(o)}</button>`).join(' ');
  return `<div class="card qcard" style="margin:6px 0">
    <div class="row" style="justify-content:space-between"><b>${esc(q.app_id)}</b><span class="tag">${esc(q.kind)}</span></div>
    <div style="margin:6px 0">${esc(q.prompt)}</div>
    <div class="muted" style="font-size:11px">📍 ${loc}${ctx.category?' · '+esc(ctx.category):''}${oldNew.length?' · '+oldNew.join(' '):''}</div>
    <div class="row" style="margin-top:8px;gap:6px">
      <input id="qa-${q.id}" placeholder="answer…" style="flex:1;min-width:160px" onkeydown="if(event.key==='Enter')answerQuestion('${q.id}',this.value)"/>
      <button class="primary sm" onclick="answerQuestion('${q.id}',document.getElementById('qa-${q.id}').value)">Answer</button>
      <button class="sm" onclick="skipQuestion('${q.id}')">Skip</button>
    </div>
    ${opts?`<div class="row" style="margin-top:6px;gap:6px"><span class="muted">suggested:</span>${opts}</div>`:''}
  </div>`;
}

/* ---------- 7R strategy (AI assigns 6R + rehost-container per app) ---------- */
// _7R_COLORS lives in app.js now (shared with server-drawer.js — both used to
// redeclare it, which aborted the script).
function _strategyBadge(s){
  if(!s) return '<span class="muted">—</span>';
  const c = _7R_COLORS[s] || 'var(--fg)';
  return `<span style="color:${c};font-weight:600">${esc(s)}</span>`;
}
let _batchAbort = null;
async function assignStrategyAll(){
  const out = $('wlScanOut');
  const apply = $('srApply') && $('srApply').checked;
  // streamed from the backend (NDJSON): one result per app, live progress, cancellable.
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
  if(_wl) refreshWorkload();   // refresh the loaded workload's 7R row if any
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

/* relative age from an ISO timestamp — "12s ago" / "3m ago" / "2h ago" / "4d ago". */
function _ago(iso){
  if(!iso) return '';
  const t = new Date(iso);
  if(isNaN(t.getTime())) return '';
  const s = Math.max(0, Math.round((Date.now()-t.getTime())/1000));
  if(s<60) return s+'s ago';
  if(s<3600) return Math.round(s/60)+'m ago';
  if(s<86400) return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
}

/* executor connectivity badge — "connected" means the executor polled
   idc-migrate recently (inbound activity), NOT that idc-migrate reached it.
   idc-migrate never initiates a connection, so there is no up/down probe. */
async function loadExecStatus(){
  const el = $('execStatus'); if(!el) return;
  el.innerHTML = '<span class="spinner"></span>';
  try{
    const s = await api('/executor/status');
    const ago = _ago(s.last_seen);
    let color, label;
    if(s.connected){ color='var(--green)'; label=`● connected${ago?` · ${ago}`:''}`; }
    else if(s.last_seen){ color='var(--amber)'; label=`● idle${ago?` · ${ago}`:''}`; }
    else if(s.token_set){ color='var(--fg)'; label='○ waiting · no executor polling yet'; }
    else { color='var(--fg)'; label='○ not configured (set a token — IDC_EXECUTOR_TOKEN)'; }
    const tok = s.token_set ? '' : ' · token unset';
    const dis = (s.enabled===false) ? ' · disabled' : '';
    el.innerHTML = `<span style="color:${color}">${label}${tok}${dis}</span>`;
    el.title = s.detail || '';
  }catch(e){ el.innerHTML = `<span style="color:var(--amber)">● status unavailable</span>`; }
}

/* ---------- Manage executors (registry: one default + N named) ---------- */
function _execBadge(e){
  // pull mode: "connected" = the executor polled idc-migrate recently (inbound
  // activity), not a probe result — idc-migrate never initiates a connection.
  // PENDING rows render their own "pending approval" tag and never reach here.
  if(!e || !e.status) return '<span class="muted">—</span>';
  const s = e.status;
  const ago = _ago(s.last_seen);
  if(s.connected) return `<span style="color:var(--green)">● connected${ago?` · ${ago}`:''}</span>`;
  if(s.last_seen) return `<span style="color:var(--amber)">● idle${ago?` · ${ago}`:''}</span>`;
  if(e.token_set) return `<span class="muted">○ waiting · no poll yet</span>`;
  return `<span class="muted">○ no token</span>`;
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
      <input id="ex_${attr(id)}_pub" value="${attr(e.public_url||'')}" placeholder="idc-migrate public URL (https://mig.zaymuc.com)" style="flex:1 1 360px;min-width:240px" title="IDC_PUBLIC_URL — idc-migrate's OWN address; every executor pushes results back here over the internet. NOT the executor's URL (idc-migrate never calls the executor)."/>
      <span class="muted" style="font-size:12px">idc-migrate public URL (IDC_PUBLIC_URL) = push-back target for ALL executors</span>
    </div>` : '';
  // PENDING self-enrollment: no token yet, can't claim/push. Operator only
  // approves (mints + reveals the token once) or rejects. No Save/Test inputs —
  // the executor owns its url; the operator only gates it. (There is no Test
  // button anywhere in pull mode — idc-migrate never initiates a connection.)
  if(e.approval === 'pending'){
    return `<div class="mcard" style="margin:6px 0;padding:8px 10px;border-color:var(--amber)">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
        <b>${esc(id)}</b>
        <span class="tag" style="color:var(--amber);border-color:var(--amber)">pending approval</span>
        <span class="muted" style="font-size:12px">self-enrolled · awaiting operator approval</span>
      </div>
      <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center">
        <span class="muted" style="font-size:12px">url: ${esc(e.url||'—')}</span>
        <span class="muted" style="font-size:12px">timeout: ${e.timeout||600}s</span>
        <button class="sm primary" onclick="approveExecutor('${attr(id)}')">Approve</button>
        <button class="sm" onclick="deleteExecutor('${attr(id)}')">Reject</button>
      </div>
      <div class="out" id="ex_${attr(id)}_out" style="margin-top:4px"></div>
    </div>`;
  }
  // approved named executors can rotate their (server-minted) token
  const rotate = e.default ? '' :
    `<button class="sm" onclick="rotateExecutorToken('${attr(id)}')" title="re-issue this executor's token (invalidates the old one)">rotate token</button>`;
  return `<div class="mcard" style="margin:6px 0;padding:8px 10px">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
      <b>${esc(id)}</b> ${tag} ${_execBadge(e)}
      <span class="muted" style="font-size:12px">${esc((e.status||{}).detail||'')}</span>
    </div>
    <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center">
      <input id="ex_${attr(id)}_tok" type="password" placeholder="token ${e.token_set?'(set · blank=keep)':'(unset)'}" style="flex:0 1 200px;min-width:140px"/>
      <label class="muted" style="white-space:nowrap;font-size:12px"><input type="checkbox" id="ex_${attr(id)}_en" ${e.enabled?'checked':''}> enabled</label>
      <input id="ex_${attr(id)}_to" type="number" min="1" value="${e.timeout||600}" style="width:70px" title="timeout (s)"/>
      <button class="sm primary" onclick="saveExecutor('${attr(id)}')">Save</button>
      ${rotate}
      ${del}
    </div>
    ${pub}
    <div class="out" id="ex_${attr(id)}_out" style="margin-top:4px"></div>
  </div>`;
}
function _showOneTimeToken(id, tok, label){
  // Render the server-minted token ONCE with a copy button + a loud warning.
  // The token is never returned again by the API, so the operator must grab
  // it here and relay it to the executor out-of-band.
  const out=$(`ex_${id}_out`); if(!out) return;
  out.innerHTML = `<div style="border:1px solid var(--amber);background:var(--amber-bg,rgba(255,180,0,.08));border-radius:6px;padding:8px">
    <div style="font-size:12px;color:var(--amber)"><b>⚠ ${label} — shown once.</b> Copy it now and relay it to the executor out-of-band (it is never returned again).</div>
    <div class="row" style="flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px">
      <code style="flex:1 1 auto;word-break:break-all;background:var(--card2,#f4f4f4);padding:4px 6px;border-radius:4px">${esc(tok)}</code>
      <button class="sm primary" onclick="navigator.clipboard.writeText('${attr(tok)}').then(()=>toast('token copied','ok'))">copy</button>
    </div></div>`;
}
function _execBody(id){
  // pull mode: idc-migrate never calls the executor, so there is no executor
  // URL to save — only the token (auth), enabled, timeout, and (default only)
  // idc-migrate's own public URL that executors push back to.
  const body = {enabled:$(`ex_${id}_en`).checked,
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
async function deleteExecutor(id){
  if(!confirm(`Delete executor "${id}"?`)) return;
  try{
    await api('/executors/'+encodeURIComponent(id), {method:'DELETE'});
    toast('executor deleted','ok');
    loadExecutors(); populateExecPickers();
  }catch(e){ toast('delete failed: '+e,'err'); }
}
async function approveExecutor(id){
  if(!confirm(`Approve executor "${id}"? idc-migrate will mint its token and show it once.`)) return;
  try{
    const e = await api('/executors/'+encodeURIComponent(id)+'/approve', {method:'POST'});
    _showOneTimeToken(id, e.token, 'executor token');
    toast('executor approved — token shown below','ok');
    populateExecPickers();
    // keep the token panel visible; do NOT reloadExecutors yet or it vanishes.
    // The operator clicks Approve on the list again to refresh after copying.
  }catch(e){ toast('approve failed: '+e,'err'); }
}
async function rotateExecutorToken(id){
  if(!confirm(`Rotate the token for executor "${id}"? The old token is invalidated immediately; the new one is shown once.`)) return;
  try{
    const e = await api('/executors/'+encodeURIComponent(id)+'/rotate-token', {method:'POST'});
    _showOneTimeToken(id, e.token, 'new executor token');
    toast('token rotated — new token shown below','ok');
  }catch(e){ toast('rotate failed: '+e,'err'); }
}
async function addExecutor(){
  // pull mode: no executor URL — idc-migrate never calls the executor. A
  // named executor only needs an id + a token (what it polls /claim with).
  const id=$('newExecId').value.trim();
  if(!id){ toast('id required','warn'); return; }
  const body={enabled:$('newExecEnabled').checked, timeout:parseInt($('newExecTimeout').value,10)||600};
  const tok=$('newExecToken').value; if(tok) body.token=tok;
  try{
    await api('/executors/'+encodeURIComponent(id), {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast('executor added','ok');
    $('newExecId').value=''; $('newExecToken').value='';
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

/* ---------- Executor management page (entrance from Scan & Migrate top) -----
   Not a top-nav tab — reached via the "Manage executors →" banner on Scan &
   Migrate. Holds everything about executors (registry, connection model,
   contract guide, status overview) so the Scan & Migrate page stays focused
   on triggers + results. Navigation mirrors the tab router but without a nav
   tab, and a back button returns to Scan & Migrate. */
function _showOnlySection(secId){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tabsec').forEach(x=>x.classList.add('hidden'));
  $(secId).classList.remove('hidden');
}
function goExecMgmt(){
  _showOnlySection('tab-execmgmt');
  loadExecMgmt();
  window.scrollTo(0,0);
}
function backToCode(){
  document.querySelectorAll('.tabsec').forEach(x=>x.classList.add('hidden'));
  $('tab-code').classList.remove('hidden');
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  const t = document.querySelector('.tab[data-tab="code"]'); if(t) t.classList.add('active');
  loadCode();
  window.scrollTo(0,0);
}
function loadExecMgmt(){
  loadExecutors();      // registry list (#execList)
  loadExecOverview();   // status summary (#mgmtExecOverview)
  loadExecJobs();       // recent /change-jobs (moved here from Scan & Migrate)
}
async function loadExecOverview(){
  const el = $('mgmtExecOverview'); if(!el) return;
  el.innerHTML = '<span class="spinner"></span> loading…';
  let st=null, list=[];
  try{ st = await api('/executor/status'); }catch(e){ st=null; }
  try{ list = await api('/executors'); }catch(e){ list=[]; }
  const approved = list.filter(e=>e.approval!=='pending');
  const pending  = list.filter(e=>e.approval==='pending');
  const enabled  = approved.filter(e=>e.enabled!==false).length;
  let dflt = '<span class="muted">—</span>';
  if(st){
    const ago = _ago(st.last_seen);
    if(st.connected) dflt = `<span style="color:var(--green)">● connected${ago?` · ${ago}`:''}</span>`;
    else if(st.last_seen) dflt = `<span style="color:var(--amber)">● idle${ago?` · ${ago}`:''}</span>`;
    else if(st.token_set) dflt = `<span class="muted">○ waiting · no poll yet</span>`;
    else dflt = `<span class="muted">○ not configured (IDC_EXECUTOR_TOKEN)</span>`;
  }
  const pub = (list.find(e=>e.default)||{}).public_url || '—';
  // at-a-glance roster of every registered executor (default + named + pending).
  // The registry card below holds the management forms; this is the read-only list.
  const rows = list.map(e=>{
    const typeTag = e.default
      ? '<span class="tag" style="color:var(--accent);border-color:var(--accent)">default</span>'
      : '<span class="muted">named</span>';
    const appr = e.approval==='pending'
      ? '<span class="tag" style="color:var(--amber);border-color:var(--amber)">pending</span>'
      : '<span style="color:var(--green)">approved</span>';
    const en = e.enabled===false
      ? '<span class="muted">off</span>'
      : '<span style="color:var(--green)">on</span>';
    return `<tr>
      <td><b>${esc(e.id)}</b></td>
      <td>${typeTag}</td>
      <td>${appr}</td>
      <td>${en}</td>
      <td>${_execBadge(e)}</td>
      <td class="muted" style="font-size:11px;word-break:break-all">${esc(e.url||'—')}</td>
    </tr>`;
  }).join('');
  const listTbl = `<div style="margin-top:14px">
      <div class="muted" style="font-size:12px;margin-bottom:4px">Executor list <span class="muted">(approve pending &amp; edit tokens in the registry card below)</span></div>
      <div class="xscroll"><table class="tbl mcard"><thead><tr>
        <th>id</th><th>type</th><th>approval</th><th>enabled</th><th>connected</th><th>url</th>
      </tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">no executors registered — add one in the registry card below.</td></tr>'}</tbody></table></div>
    </div>`;
  el.innerHTML = `<div class="row" style="flex-wrap:wrap;gap:18px;align-items:center">
      <div><span class="muted" style="font-size:11px">default executor</span><br><b>${dflt}</b></div>
      <div><span class="muted" style="font-size:11px">approved</span><br><b>${approved.length}</b> <span class="muted">(${enabled} enabled)</span></div>
      <div><span class="muted" style="font-size:11px">pending approval</span><br><b style="color:${pending.length?'var(--amber)':'var(--fg)'}">${pending.length}</b></div>
      <div><span class="muted" style="font-size:11px">push-back public URL</span><br><code style="word-break:break-all">${esc(pub)}</code></div>
    </div>${listTbl}`;
}
/* Fill the shared #appList datalist from /api/apps so every app_id input
   (the Scan & Migrate workload picker + the Code tab's App → sources picker)
   autocompletes from the full workloads catalog instead of freehand. The
   option label carries the app name so the picker is readable. */
async function populateAppPickers(){
  let apps; try{ apps = await api('/apps'); }catch(e){ return; }
  const dl = $('appList'); if(!dl) return;
  dl.innerHTML = apps.map(a=>`<option value="${attr(a.app_id)}">${esc(a.name||a.app_id)}</option>`).join('');
}

/* ---------- Repos (git urls) — first-class, N:N with hosts ----------
   A repo is a global url-unique git url. Host↔repo is N:N (a host runs several
   repos, a repo is deployed on several hosts). An app's repos are derived from
   its hosts (app→hosts→repos), so the operator maps repos to HOSTS, not apps. */
async function loadRepos(){
  const tb = $('repoTbl'); if(!tb) return;
  const body = tb.querySelector('tbody'); body.innerHTML = '<tr><td colspan="6" class="muted">loading…</td></tr>';
  let list; try{ list = await api('/repos'); }
  catch(e){ body.innerHTML = `<tr><td colspan="6" class="ev-err">load failed: ${esc(e)}</td></tr>`; return; }
  window._repoMap = {}; list.forEach(r=> _repoMap[r.repo_id] = r);   // for the chips editor
  if(!list.length){ body.innerHTML = '<tr><td colspan="6" class="muted">no repos yet — add one above, or scan a git group.</td></tr>'; return; }
  body.innerHTML = list.map(_repoRow).join('');
}
function _repoRow(r){
  const apps = (r.apps||[]).length
    ? (r.apps.slice(0,6).map(a=>`<span class="tag">${esc(a)}</span>`).join(' ') + (r.apps.length>6?` <span class="muted">+${r.apps.length-6}</span>`:''))
    : '<span class="muted">—</span>';
  const hostTxt = (r.hosts||[]).length
    ? `${r.host_count} · ${r.hosts.slice(0,5).map(esc).join(', ')}${r.hosts.length>5?` +${r.hosts.length-5}`:''}`
    : '<span class="muted">0</span>';
  return `<tr>
    <td data-label="repo"><b>${esc(r.name||r.url)}</b><br><span class="muted" style="font-size:11px;word-break:break-all">${esc(r.url)}</span></td>
    <td data-label="branch">${esc(r.branch||'—')}</td>
    <td data-label="hosts">${hostTxt}</td>
    <td data-label="apps">${apps}</td>
    <td data-label="edit hosts"><button class="sm" onclick="editRepoHosts('${attr(r.repo_id)}')">edit hosts</button></td>
    <td data-label="delete"><button class="sm" onclick="deleteRepo('${attr(r.repo_id)}')">delete</button></td>
  </tr>`;
}
async function addRepo(){
  const url=$('repoUrl').value.trim();
  if(!url){ toast('enter a git url','warn'); return; }
  const body={url, branch:$('repoBranch').value.trim()};
  const nm=$('repoName').value.trim(); if(nm) body.name=nm;
  try{
    await api('/repos', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    toast('repo added','ok');
    $('repoUrl').value=''; $('repoBranch').value=''; $('repoName').value='';
    loadRepos();
  }catch(e){ toast('add failed: '+e,'err'); }
}
async function deleteRepo(id){
  if(!confirm('Delete this repo and its host links?')) return;
  try{ await api('/repos/'+encodeURIComponent(id), {method:'DELETE'}); toast('repo deleted','ok'); loadRepos(); }
  catch(e){ toast('delete failed: '+e,'err'); }
}
/* edit-hosts chips editor (replaces the old comma-separated prompt(): a list
   deserves a real list control — removable chips + add-by-hostname + save). */
let _rhe = { id: null, name: '', hosts: [] };
async function editRepoHosts(id){
  let cur; try{ cur = await api('/repos/'+encodeURIComponent(id)+'/hosts'); }
  catch(e){ toast('load failed: '+e,'err'); return; }
  const r = (window._repoMap || {})[id] || {};
  _rhe = { id, name: r.name || r.url || id, hosts: cur.map(h=>h.hostname||h.server_id) };
  $('rheName').textContent = _rhe.name;
  _renderRheChips();
  $('rheAdd').value=''; $('rheOut').innerHTML='';
  $('repoHostEditor').classList.remove('hidden');
  $('repoHostEditor').scrollIntoView({behavior:'smooth', block:'nearest'});
}
function _renderRheChips(){
  const box = $('rheChips'); if(!box) return;
  if(!_rhe.hosts.length){ box.innerHTML = '<span class="muted" style="font-size:12px">no hosts yet — add one below.</span>'; return; }
  box.innerHTML = _rhe.hosts.map(h=>`<span class="tag" style="display:inline-flex;align-items:center;gap:4px">${esc(h)} <button class="sm" style="padding:0 4px;line-height:1" onclick="rheRemoveChip('${attr(h)}')">×</button></span>`).join('');
}
function rheAddChip(){
  const v = $('rheAdd').value.trim();
  if(!v){ return; }
  if(_rhe.hosts.includes(v)){ toast('already in the list','warn'); return; }
  _rhe.hosts.push(v);
  $('rheAdd').value='';
  _renderRheChips();
}
function rheRemoveChip(h){
  _rhe.hosts = _rhe.hosts.filter(x=>x!==h);
  _renderRheChips();
}
async function rheSave(){
  if(!_rhe.id){ return; }
  $('rheOut').innerHTML = '<span class="spinner"></span>';
  try{
    const res = await api('/repos/'+encodeURIComponent(_rhe.id)+'/hosts', {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify({hosts: _rhe.hosts})});
    if(res.unresolved && res.unresolved.length) toast(`${res.matched} linked · ${res.unresolved.length} unresolved: ${res.unresolved.join(', ')}`, 'warn');
    else toast(`${res.matched} host(s) linked`,'ok');
    rheCancel();
    loadRepos();
  }catch(e){ $('rheOut').innerHTML = `<span class="ev-err">save failed: ${esc(e)}</span>`; }
}
function rheCancel(){
  _rhe = { id: null, name: '', hosts: [] };
  $('repoHostEditor')?.classList.add('hidden');
}

/* host typeahead — pick a host from the estate instead of typing freehand.
   Powers the chips-editor #rheAdd (adds the chip on pick) and the Host→sources
   #hrHost. Debounced 200ms, GET /api/hosts/suggest?q=<substr>&limit=15. Free-text
   still works for typos/server_ids (the server resolves + reports unmatched). */
let _hsugTimer = null;
function _hsugWire(inputId, boxId, onPick){
  const inp = $(inputId);
  if(!inp || inp.dataset.hsug) return;
  inp.dataset.hsug = '1';
  inp.addEventListener('input', ()=>{
    clearTimeout(_hsugTimer);
    const v = inp.value.trim();
    if(v.length < 1){ $(boxId)?.classList.add('hidden'); return; }
    _hsugTimer = setTimeout(()=> _hsugFetch(inputId, boxId), 200);
  });
  // hide on blur (delayed so a mousedown pick lands first)
  inp.addEventListener('blur', ()=> setTimeout(()=> $(boxId)?.classList.add('hidden'), 150));
}
async function _hsugFetch(inputId, boxId){
  const v = $(inputId).value.trim();
  if(!v){ $(boxId)?.classList.add('hidden'); return; }
  let list; try{ list = await api('/hosts/suggest?q='+encodeURIComponent(v)+'&limit=15'); }
  catch(e){ return; }
  const box = $(boxId); if(!box) return;
  if(!list.length){ box.classList.add('hidden'); return; }
  box.innerHTML = list.map(h=>{
    const label = h.hostname || h.server_id;
    const meta = [h.role, h.fqdn].filter(Boolean).join(' · ');
    return `<div style="padding:5px 8px;cursor:pointer;border-bottom:1px solid var(--border)" onmouseover="this.style.background='rgba(0,0,0,.06)'" onmouseout="this.style.background=''" onmousedown="_hsugPick('${attr(inputId)}','${attr(boxId)}','${attr(label)}');return false"><b>${esc(label)}</b> <span class="muted" style="font-size:11px">${esc(meta)}</span></div>`;
  }).join('');
  box.classList.remove('hidden');
}
function _hsugPick(inputId, boxId, label){
  $(inputId).value = label;
  $(boxId)?.classList.add('hidden');
  if(inputId === 'rheAdd') rheAddChip();   // chips editor: add immediately on pick
}

/* ---------- Host → sources (the other side of the host↔repo N:N) ----------
   The "Code" tab's second card: pick a host, see/edit the sources it deploys.
   Add/Remove each re-PUTs the host's full source set immediately. */
let _hrState = { sid: null, repos: [] };   // current host's server_id + repo_id set

async function loadSources(){
  // the "Code" tab loader: source catalog + repo-id picker + app picker.
  loadRepos();
  populateAppPickers();   // #appList datalist for the App→sources picker
  _hsugWire('rheAdd', 'rheSuggest');   // chips-editor host typeahead
  _hsugWire('hrHost', 'hrSuggest');    // Host→sources host typeahead
  // populate #repoIdList (options carry repo_id as value, name+url as label)
  let list; try{ list = await api('/repos'); }catch(e){ return; }
  const dl = $('repoIdList'); if(dl) dl.innerHTML = list.map(r=>`<option value="${attr(r.repo_id)}">${esc(r.name||r.url)} — ${esc(r.url)}</option>`).join('');
}

/* ---------- App → sources (the migration-centric primary view) ----------
   An app's sources are derived from its hosts. Pick an app, see its sources +
   which hosts carry each, add a source (links to every host in the app) or
   remove one (unlinks from every host). Backed by GET/PUT /api/apps/{id}/repos. */
let _arState = { app: '', sourceIds: [] };

async function loadAppSources(){
  const app = $('arApp').value.trim();
  if(!app){ toast('enter an app id','warn'); return; }
  $('arOut').innerHTML = '<span class="spinner"></span>';
  let res; try{ res = await api('/apps/'+encodeURIComponent(app)+'/sources'); }
  catch(e){ $('arResult')?.classList.add('hidden'); $('arOut').innerHTML = `<span class="ev-err">${esc(e)}</span>`; return; }
  _arState = { app, sourceIds: (res.sources||[]).map(s=>s.repo_id) };
  _renderAppSources(res);
  $('arResult').classList.remove('hidden');
  $('arAppLabel').textContent = app;
  $('arMeta').textContent = `${res.host_total} host(s) in app · ${(res.sources||[]).length} source(s)`;
  $('arOut').innerHTML = '';
}

function _renderAppSources(res){
  const tb = $('arTbl').querySelector('tbody');
  const srcs = res.sources || [];
  if(!srcs.length){
    tb.innerHTML = '<tr><td colspan="5" class="muted">no sources mapped to this app yet. add one below (pick from the catalog), or scan a git group first.</td></tr>';
    $('arHint').textContent = '';
    return;
  }
  $('arHint').textContent = 'add maps the source to every host in the app; remove unlinks it from every host.';
  tb.innerHTML = srcs.map(s=>{
    const hosts = (s.hostnames||[]).length
      ? `${s.host_count} · ${s.hostnames.slice(0,5).map(esc).join(', ')}${s.hostnames.length>5?` +${s.hostnames.length-5}`:''}`
      : '<span class="muted">0</span>';
    return `<tr>
      <td data-label="source"><b>${esc(s.name||s.url)}</b></td>
      <td data-label="url" style="font-size:11px;word-break:break-all">${esc(s.url)}</td>
      <td data-label="branch">${esc(s.branch||'—')}</td>
      <td data-label="hosts">${hosts}</td>
      <td data-label="remove"><button class="sm" onclick="removeAppRepo('${attr(s.repo_id)}')">remove</button></td>
    </tr>`;
  }).join('');
}

async function addAppRepo(){
  if(!_arState.app){ toast('load an app first','warn'); return; }
  const rid = $('arAddRepo').value.trim();
  if(!rid){ toast('pick a source to add','warn'); return; }
  if(_arState.sourceIds.includes(rid)){ toast('already on this app','warn'); return; }
  try{
    const res = await api('/apps/'+encodeURIComponent(_arState.app)+'/repos', {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify({repo_id: rid, action:'add'})});
    toast(`linked to ${res.hosts_changed} host(s)${res.hosts_skipped?` · ${res.hosts_skipped} already had it`:''}`, 'ok');
    $('arAddRepo').value='';
    loadAppSources();
    loadRepos();
  }catch(e){ toast('add failed: '+e,'err'); }
}

async function removeAppRepo(rid){
  if(!_arState.app){ return; }
  if(!confirm('Remove this source from every host in the app?')) return;
  try{
    const res = await api('/apps/'+encodeURIComponent(_arState.app)+'/repos', {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify({repo_id: rid, action:'remove'})});
    toast(`unlinked from ${res.hosts_changed} host(s)`, 'ok');
    loadAppSources();
    loadRepos();
  }catch(e){ toast('remove failed: '+e,'err'); }
}

async function loadHostRepos(){
  const tok = $('hrHost').value.trim();
  if(!tok){ toast('enter a hostname or server id','warn'); return; }
  let list; try{ list = await api('/hosts/'+encodeURIComponent(tok)+'/repos'); }
  catch(e){ $('hrResult')?.classList.add('hidden'); toast('load failed: '+e,'err'); return; }
  // resolve the token to a server_id via the host-status-ish read; the GET
  // returned repos but not the resolved id — re-derive from the token by
  // asking for the host's repos is enough; store the token as the identity.
  _hrState = { sid: tok, repos: list.map(r=>r.repo_id) };
  _renderHostRepos(list);
  $('hrResult')?.classList.remove('hidden');
  $('hrHostLabel').textContent = tok;
  $('hrHostMeta').textContent = `${list.length} source${list.length===1?'':'s'}`;
}

function _renderHostRepos(list){
  const tb = $('hrTbl')?.querySelector('tbody'); if(!tb) return;
  if(!list.length){ tb.innerHTML = '<tr><td colspan="4" class="muted">no sources mapped to this host yet.</td></tr>'; return; }
  tb.innerHTML = list.map(r=>`<tr>
    <td data-label="source"><b>${esc(r.name||r.url)}</b></td>
    <td data-label="url" style="font-size:11px;word-break:break-all">${esc(r.url)}</td>
    <td data-label="branch">${esc(r.branch||'—')}</td>
    <td data-label="remove"><button class="sm" onclick="removeHostRepo('${attr(r.repo_id)}')">remove</button></td>
  </tr>`).join('');
}

async function addHostRepo(){
  if(!_hrState.sid){ toast('load a host first','warn'); return; }
  const rid = $('hrAddRepo').value.trim();
  if(!rid){ toast('pick a source to add','warn'); return; }
  const next = [...new Set([..._hrState.repos, rid])];
  await _putHostRepos(next, () => { $('hrAddRepo').value=''; });
}

async function removeHostRepo(rid){
  const next = _hrState.repos.filter(x=>x!==rid);
  await _putHostRepos(next);
}

async function _putHostRepos(repoIds, afterOk){
  try{
    const res = await api('/hosts/'+encodeURIComponent(_hrState.sid)+'/repos',
      {method:'PUT', headers:{'content-type':'application/json'}, body:JSON.stringify({repo_ids: repoIds})});
    if(res.unresolved && res.unresolved.length) toast(`${res.linked} linked · ${res.unresolved.length} unknown repo id(s)`, 'warn');
    else toast(`${res.linked} source(s) on this host`,'ok');
    _hrState.repos = repoIds.filter(r=> !res.unresolved || !res.unresolved.includes(r));
    // refresh the host's source list display
    const list = await api('/hosts/'+encodeURIComponent(_hrState.sid)+'/repos');
    _renderHostRepos(list);
    $('hrHostMeta').textContent = `${list.length} source${list.length===1?'':'s'}`;
    loadRepos();   // the catalog's host counts/apps changed too
    if(afterOk) afterOk();
  }catch(e){ toast('save failed: '+e,'err'); }
}

/* ---------- Scan a git group (discover-repos executor action) ----------
   The operator pastes a git GROUP/ORG url; idc-migrate enqueues a discover-repos
   task, the executor enumerates the repositories inside it (with its own git/SCM
   creds — idc-migrate never touches git) and pushes the list back. We poll the
   scan until done, then render a checkbox list so the operator can pick which
   repos to register as first-class sources. */
let _discState = { scanId: null, url: '', repos: [] };
const _DISC_POLL_MS = 1500, _DISC_POLL_MAX = 80;   // ~2 min before we stop polling

async function scanRepos(){
  const url = $('discUrl').value.trim();
  if(!url){ toast('enter a git group / org url','warn'); return; }
  // guard: if no executor is connected the scan will sit pending forever —
  // warn up front (still allow enqueueing; the operator may be about to start one).
  let connected = null;
  try{ const s = await api('/executor/status'); connected = !!s.connected; }catch(e){}
  if(connected === false){
    $('discOut').innerHTML = '<span class="ev-err">⚠ no executor connected — the scan will be queued but no executor is polling to pick it up. Register/approve an executor with git access, then re-scan.</span>';
  }else{
    $('discOut').innerHTML = '<span class="spinner"></span> <span class="muted">scanning…</span>';
  }
  $('discResult')?.classList.remove('hidden');
  $('discUrlLabel').textContent = url;
  $('discMeta').textContent = 'scan queued — waiting for an executor to pick it up…';
  $('discTbl').querySelector('tbody').innerHTML = '';
  try{
    const r = await api('/repos/discover', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({url})});
    _discState = { scanId: r.scan_id, url, repos: [] };
    _discPoll(r.scan_id, 0);
  }catch(e){
    $('discOut').innerHTML = `<span class="ev-err">scan failed: ${esc(e)}</span>`;
    $('discMeta').textContent = '';
  }
}

async function _discPoll(scanId, attempts){
  let sc;
  try{ sc = await api('/repos/discover/'+encodeURIComponent(scanId)); }
  catch(e){ $('discOut').innerHTML = `<span class="ev-err">poll failed: ${esc(e)}</span>`; return; }
  if(sc.status === 'pending'){
    if(attempts >= _DISC_POLL_MAX){
      $('discMeta').textContent = 'still pending — no executor picked this up.';
      $('discOut').innerHTML = '<span class="ev-err">no executor claimed the scan in time. Is an executor registered and polling? Start/approve one, then re-scan.</span>';
      return;   // stop polling — don't spin forever
    }
    $('discMeta').textContent = `scan running — waiting for the executor… (${attempts+1})`;
    setTimeout(()=>_discPoll(scanId, attempts+1), _DISC_POLL_MS);
    return;
  }
  if(sc.status === 'error'){
    $('discOut').innerHTML = `<span class="ev-err">scan error: ${esc(sc.error||'unknown')}</span>`;
    $('discMeta').textContent = 'error';
    return;
  }
  // done — render the discovered repos as a checkbox list
  _discState = { scanId, url: sc.url, repos: sc.repos || [] };
  $('discOut').innerHTML = '';
  _renderDiscovered(_discState.repos);
}

function _renderDiscovered(repos){
  const tb = $('discTbl').querySelector('tbody');
  $('discMeta').textContent = `${repos.length} repos discovered — pick the ones to register.`;
  $('discHint').textContent = repos.length ? 'already-registered urls are skipped (they are shared sources).' : '';
  if(!repos.length){
    tb.innerHTML = '<tr><td colspan="4" class="muted">no repositories found under this url.</td></tr>';
    return;
  }
  tb.innerHTML = repos.map((r,i)=>`<tr>
    <td data-label="pick"><input type="checkbox" class="discChk" data-i="${i}" checked/></td>
    <td data-label="repo"><b>${esc(r.name||r.url)}</b><br><span class="muted" style="font-size:11px;word-break:break-all">${esc(r.url)}</span></td>
    <td data-label="branch">${esc(r.branch||'main')}</td>
    <td data-label="desc" class="conf" title="${esc(r.description||'')}">${esc(r.description||'—')}</td>
  </tr>`).join('');
  $('discAll').checked = true;
}

function discToggleAll(cb){
  document.querySelectorAll('.discChk').forEach(c=> c.checked = cb.checked);
}

async function registerDiscovered(){
  const picks = [];
  document.querySelectorAll('.discChk').forEach(c=>{
    if(c.checked){
      const r = _discState.repos[Number(c.dataset.i)];
      if(r) picks.push({url: r.url, branch: r.branch||'', name: r.name||''});
    }
  });
  if(!picks.length){ toast('select at least one repo','warn'); return; }
  $('discOut').innerHTML = '<span class="spinner"></span> <span class="muted">registering…</span>';
  try{
    const res = await api('/repos/bulk', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({repos: picks})});
    const parts = [`${res.created_count} registered`];
    if((res.skipped||[]).length) parts.push(`${res.skipped.length} already registered`);
    if((res.invalid||[]).length) parts.push(`${res.invalid.length} invalid`);
    toast(parts.join(' · '), res.created_count ? 'ok' : 'info');
    $('discOut').innerHTML = `<span class="muted">${esc(parts.join(' · '))}.</span>`;
    loadRepos();   // refresh the source catalog below
    // also refresh the host→source repo-id picker
    let list; try{ list = await api('/repos'); }catch(e){ list = []; }
    const dl = $('repoIdList'); if(dl) dl.innerHTML = list.map(r=>`<option value="${attr(r.repo_id)}">${esc(r.name||r.url)} — ${esc(r.url)}</option>`).join('');
  }catch(e){
    $('discOut').innerHTML = `<span class="ev-err">register failed: ${esc(e)}</span>`;
  }
}

/* ---------- F5/F6 — DB conversion profiles + convert mode ---------- */
function _gradeColor(g){ return g==='A'?'var(--green)':g==='B'?'var(--amber)':g==='C'?'var(--red)':'var(--fg)'; }
async function showDbConv(dbServerId){
  const el = $('wlDbConvDetail');
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

/* ---------- F5 — IaC + Well-Architected guardrails ---------- */
async function showIac(scopeId){
  const el = $('wlIacDetail'); el.classList.remove('hidden'); el.innerHTML='<span class="spinner"></span>';
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
/* shared disposition color map (legacy/EOL hosts) */
const _LD_COLOR = {containerize:'var(--green)', replatform:'#3b8eea', rewrite:'var(--amber)', retain:'#888', retire:'#e5484d'};
