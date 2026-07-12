/* ---------- LLM wave planning (Waves tab) ----------
 * POST /api/plan/propose → dry-run waves + validation; POST /api/plan/apply
 * to persist (replaces existing waves). Surfaces the plan-llm feature that
 * previously only had a backend + CLI. */
let _proposedWaves = null;   // last proposed wave list (for Apply)

async function proposePlan(){
  const demand = $('planDemand').value.trim();
  if(!demand){ $('planOut').innerHTML = '<span class="ev-err">enter a demand first</span>'; return; }
  // max_waves is optional; omit from the body when blank so the backend only
  // auto-parses a cap from the demand text.
  const mw = $('planMaxWaves').value.trim();
  const body = {demand, scope:'all'};
  if(mw && Number(mw) > 0) body.max_waves = Number(mw);
  $('planOut').innerHTML = '<span class="spinner"></span> asking MigraQ to design a policy…';
  let r; try{
    r = await api('/plan/propose', {method:'POST', headers:{'content-type':'application/json'},
              body:JSON.stringify(body)});
  }catch(e){ $('planOut').innerHTML = '<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  if(!r.ok){
    $('planOut').innerHTML = `<span class="ev-err">MigraQ failed: ${esc((r.errors||[]).join('; '))||esc(r.raw||'')}</span>`;
    return;
  }
  _proposedWaves = r.waves || [];
  const val = r.validation || {errors:[], warnings:[]};
  const errs = (val.errors||[]).length, warns = (val.warnings||[]).length;
  const stats = r.stats || {};
  const cap = r.max_waves;
  const revs = r.revisions || [];
  const rows = (r.wave_hostnames || r.waves || []).slice(0, 200).map(w=>{
    const members = (w.members||[]).join(', ');
    const stage = w.stage || '';
    return `<tr>
      <td data-label="Wave">${esc(w.name||w.id)}</td>
      <td data-label="Stage">${esc(stage)}</td>
      <td data-label="Depends on">${esc((w.depends_on||[]).join(', ')||'-')}</td>
      <td data-label="#">${(w.server_ids||[]).length}</td>
      <td data-label="Members">${esc(members.slice(0,80))}${members.length>80?'…':''}</td>
    </tr>`;
  }).join('');
  const valHtml = [
    errs   ? `<div class="ev-err">✗ ${errs} validation error(s): ${esc((val.errors||[]).join('; '))}</div>` : '',
    warns  ? `<div style="color:var(--amber)">⚠ ${warns} warning(s): ${esc((val.warnings||[]).join('; '))}</div>` : '',
    (r.errors||[]).length ? `<div class="ev-err">policy errors: ${esc((r.errors||[]).join('; '))}</div>` : '',
  ].join('');
  // revision trail: one line per LLM re-prompt round-trip. Empty when the
  // first proposal was already within cap (or no cap was set).
  const revHtml = revs.length ? `
    <details class="mcard" style="margin:6px 0;padding:6px 10px">
      <summary class="muted" style="cursor:pointer">MigraQ revision trail — ${revs.length} round(s)${cap?` · cap ${cap}`:''}</summary>
      <div style="margin-top:4px">
        ${revs.map(rv=>{
          const before = rv.waves_before==null?'?':rv.waves_before;
          const after  = rv.waves_after==null?'?':rv.waves_after;
          const mark   = rv.accepted ? '<span style="color:var(--green)">✓ accepted</span>'
                       : `<span style="color:var(--amber)">↻ not within cap</span>`;
          const err    = rv.errors&&rv.errors.length ? ` · <span class="ev-err">${esc(rv.errors.join('; '))}</span>` : '';
          return `<div class="muted">· round ${rv.attempt}: ${before} → ${after} waves${cap?` (cap ${cap})`:''} ${mark}${err}</div>`;
        }).join('')}
      </div>
    </details>` : '';
  const capNote = cap ? ` · cap ${cap}` : '';
  $('planOut').innerHTML = `
    <div class="muted">proposed ${stats.waves||_proposedWaves.length} waves${capNote} · ${stats.assigned||0}/${stats.total||0} servers assigned · ${stats.unassigned||0} unassigned</div>
    ${revHtml}
    ${valHtml}
    <div class="row" style="margin:8px 0;gap:8px">
      <button class="primary" onclick="applyPlan()" ${errs?'disabled title="fix validation errors first"':''}>Apply (persist)</button>
      <span class="muted">Apply replaces the current waves.</span>
    </div>
    <div class="scroll" style="max-height:50vh"><table class="mcard"><thead><tr><th>wave</th><th>stage</th><th>depends_on</th><th>#</th><th>members</th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">no waves</td></tr>'}</tbody></table></div>`;
}

async function applyPlan(){
  if(!_proposedWaves || !_proposedWaves.length){ toast('propose a plan first','warn'); return; }
  $('planOut').insertAdjacentHTML('beforeend', '<span class="spinner"></span> applying…');
  let r; try{
    r = await api('/plan/apply', {method:'POST', headers:{'content-type':'application/json'},
              body:JSON.stringify({waves:_proposedWaves})});
  }catch(e){ $('planOut').innerHTML = '<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  if(!r.ok){
    $('planOut').insertAdjacentHTML('beforeend', `<div class="ev-err">apply failed: ${esc(((r.validation||{}).errors||[]).join('; '))}</div>`);
    return;
  }
  $('planOut').insertAdjacentHTML('beforeend', `<div style="color:var(--green)">✓ applied ${r.waves} wave(s)</div>`);
  loadWaves();   // refresh the current-waves list on the left
}