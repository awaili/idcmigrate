/* ---------- Readiness heatmap (F10) ---------- */
const _RD_COLOR = {green:'var(--green)', yellow:'var(--amber)', red:'var(--red)', 'n/a':'#888'};
function _rdCell(v){ const lvl=(v&&v.level)||'n/a'; const c=_RD_COLOR[lvl]||'#888';
  return `<span class="tag" style="color:${c}" title="${esc((v&&v.detail)||'')}">${esc(lvl)}</span>`; }
async function loadReadiness(){
  const out=$('readinessHeatmap');
  out.innerHTML='<span class="spinner"></span> loading readiness…';
  try{
    const rows = await api('/readiness');
    if(!rows.length){ out.innerHTML='<span class="muted">no waves — run Rebuild first</span>'; return; }
    const sigs=['lz_ready','db_conversion','code_refactor','deps_resolved','cutover_rehearsal','rollback_channel'];
    const head = `<thead><tr><th>wave</th><th>stage</th><th>servers</th>${sigs.map(s=>`<th>${esc(s.replace('_',' '))}</th>`).join('')}<th>rollup</th><th>cutover?</th></tr></thead>`;
    const body = rows.map(r=>{
      const sg=r.signals||{}; const rc=_RD_COLOR[r.rollup]||'#888';
      return `<tr><td><b>${esc((r.wave_name||r.wave_id||'').slice(0,28))}</b></td>
        <td>${esc(r.stage||'')}</td><td>${r.server_count||0}</td>
        ${sigs.map(s=>`<td style="text-align:center">${_rdCell(sg[s])}</td>`).join('')}
        <td style="text-align:center"><span class="tag" style="color:${rc}">${esc(r.rollup)}</span></td>
        <td>${r.can_cutover?'<span style="color:var(--green)">yes</span>':'<span style="color:var(--red)">no</span>'}</td></tr>`;
    }).join('');
    out.innerHTML = `<div class="xscroll"><table class="tbl mcard">${head}<tbody>${body}</tbody></table></div>`;
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
}
