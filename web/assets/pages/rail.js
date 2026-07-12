/* ---------- Migration flow rail ----------
   A persistent spine across the top of the page that turns the product from
   "a box of AI buttons" into "a guided flow with the copilot riding each step".
   Every stage's count comes from an existing deterministic endpoint (no
   inference), so the rail never lies about state. Clicking a chip jumps to the
   relevant tab and fires the natural next action (Red-team runs the AI
   red-team, Coverage applies the low-conf filter, Readiness reloads the heat). */
const RAIL_STAGES = ['ingest','match','coverage','plan','redteam','sevenr','db','exec','readiness'];
const _RAIL_TAB = {
  ingest:'dashboard', match:'dashboard', coverage:'inventory', plan:'waves',
  redteam:'waves', sevenr:'code', db:'code', exec:'execution', readiness:'readiness',
};

async function loadRail(){
  const el = $('rail'); if(!el) return;
  el.innerHTML = '<span class="rail-loading">loading flow…</span>';
  // parallel fetch — each guarded so one endpoint failure doesn't blank the rail
  const [stats, agg, strat, dbs, mjobs, rdy] = await Promise.all([
    api('/stats').catch(()=>null),
    api('/aggregations').catch(()=>null),
    api('/strategies').catch(()=>null),
    api('/db-profiles').catch(()=>null),
    api('/migration-jobs').catch(()=>null),
    api('/readiness').catch(()=>null),
  ]);
  const s = stats || {};
  const conf = (agg && agg.confidence) || {};
  const lowConf = conf['low(<0.7)'] || 0;
  const waves = s.waves || 0, matches = s.matches || 0, servers = s.servers || 0;
  const profiles = s.code_profiles || 0;
  const assigned = Array.isArray(strat) ? strat.length : 0;
  const dbN = Array.isArray(dbs) ? dbs.length : 0;
  const jobsN = Array.isArray(mjobs) ? mjobs.length : 0;
  // readiness worst-case rollup across waves (red > yellow > green)
  let rdyLevel = null;
  if(Array.isArray(rdy) && rdy.length){
    const lv = rdy.map(r => r.rollup);
    if(lv.includes('red')) rdyLevel = 'red';
    else if(lv.includes('yellow')) rdyLevel = 'yellow';
    else if(lv.every(x => x === 'green')) rdyLevel = 'green';
  }
  const stages = {
    ingest:    {label:'Ingest',     meta: servers ? `${servers} srv` : '—',          state: servers>0 ? 'done' : 'todo'},
    match:     {label:'Match',      meta: matches ? `${matches}` : '—',            state: matches>0 ? 'done' : 'todo'},
    coverage:  {label:'Coverage',   meta: matches>0 ? `${lowConf} low-conf` : '—',  state: matches>0 ? (lowConf>0 ? 'warn' : 'done') : 'todo'},
    plan:      {label:'Plan',       meta: waves ? `${waves} waves` : '—',           state: waves>0 ? 'done' : 'todo'},
    redteam:   {label:'Red-team',   meta: waves>0 ? 'run AI' : '—',                 state: waves>0 ? 'active' : 'todo'},
    sevenr:    {label:'7R',         meta: profiles>0 ? `${assigned}/${profiles}` : '—', state: profiles>0 ? (assigned>=profiles ? 'done' : 'warn') : 'todo'},
    db:        {label:'DB assess',  meta: dbN ? `${dbN} profiled` : '—',             state: dbN>0 ? 'done' : 'todo'},
    exec:      {label:'Execute',    meta: jobsN ? `${jobsN} jobs` : '—',            state: jobsN>0 ? 'active' : 'todo'},
    readiness: {label:'Readiness', meta: rdyLevel || '—',                          state: rdyLevel==='green' ? 'done' : (rdyLevel==='red' ? 'blocked' : (rdyLevel==='yellow' ? 'warn' : 'todo'))},
  };
  renderRail(stages);
}

function _railColor(state){
  return {done:'var(--green)', active:'var(--accent)', warn:'var(--amber)',
          blocked:'var(--red)', todo:'var(--muted)'}[state] || 'var(--muted)';
}

function renderRail(stages){
  const el = $('rail');
  el.innerHTML =
    '<span class="rail-title">Migration flow</span>' +
    RAIL_STAGES.map(k => {
      const st = stages[k]; const c = _railColor(st.state);
      return `<div class="rail-chip ${st.state}" onclick="railGo('${k}')" title="${esc(st.label)} — ${esc(st.meta)}">
        <span class="rail-dot" style="background:${c}"></span>
        <span class="rail-label">${esc(st.label)}</span>
        <span class="rail-meta">${esc(st.meta)}</span>
      </div>`;
    }).join('') +
    '<button class="sm rail-refresh" onclick="loadRail()" title="refresh flow status">↻</button>';
}

function goTab(name){
  const t = document.querySelector('.tab[data-tab="' + name + '"]');
  if(t) t.click();
}

function railGo(stage){
  const tab = _RAIL_TAB[stage];
  if(tab) goTab(tab);
  // fire the natural next action on the destination tab (small delay so the
  // tab's own loader runs first; the target element is always in the DOM)
  if(stage === 'redteam' && typeof reviewPlan === 'function') setTimeout(reviewPlan, 60);
  if(stage === 'coverage' && typeof applyQuick === 'function') setTimeout(() => applyQuick('lowconf'), 60);
  if(stage === 'readiness' && typeof loadReadiness === 'function') setTimeout(loadReadiness, 60);
}