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
    const sigs=['lz_ready','db_conversion','code_refactor','deps_resolved','cutover_rehearsal','rollback_channel','hw_support','os_support'];
    const mig = sigs.slice(0,6), gap = sigs.slice(6);
    const head = `<thead>
      <tr><th rowspan="2">wave</th><th rowspan="2">stage</th><th rowspan="2">servers</th>
        <th colspan="6" style="border-bottom:1px solid var(--border)">migration readiness</th>
        <th colspan="2" style="border-bottom:1px solid var(--border);color:#a06bff">data-gap (support)</th>
        <th rowspan="2">rollup</th><th rowspan="2">cutover?</th></tr>
      <tr>${sigs.map(s=>`<th style="font-size:11px">${esc(s.replace(/_/g,' '))}</th>`).join('')}</tr>
    </thead>`;
    const body = rows.map(r=>{
      const sg=r.signals||{}; const rc=_RD_COLOR[r.rollup]||'#888';
      return `<tr><td><b>${esc((r.wave_name||r.wave_id||'').slice(0,28))}</b></td>
        <td>${esc(r.stage||'')}</td><td>${r.server_count||0}</td>
        ${sigs.map(s=>`<td style="text-align:center">${_rdCell(sg[s])}</td>`).join('')}
        <td style="text-align:center"><span class="tag" style="color:${rc}">${esc(r.rollup)}</span></td>
        <td>${r.can_cutover?'<span style="color:var(--green)">yes</span>':'<span style="color:var(--red)">no</span>'}</td></tr>`;
    }).join('');
    out.innerHTML = `<div class="xscroll"><table class="tbl mcard">${head}<tbody>${body}</tbody></table></div>
      <div class="muted" style="font-size:11px;margin-top:4px">hw/os = hardware warranty + OS vendor-support (data-gap); <span style="color:#888">n/a</span> = not assessed (ignored by the rollup — surfaced on the Data Quality tab instead). red blocks cutover.</div>`;
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
}
