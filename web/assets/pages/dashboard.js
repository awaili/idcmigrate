/* ---------- dashboard ---------- */
function bars(data, colors){
  data = data || {};
  const max = Math.max(1, ...Object.values(data));
  const cmap = colors || {};
  return Object.entries(data).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
    const cls = cmap[k] ? ' '+cmap[k] : '';
    return `<div class="bar-row"><div title="${esc(k)}">${esc(k)}</div><div class="bar-track"><div class="bar-fill${cls}" style="width:${(v/max*100).toFixed(1)}%"></div></div><div class="n" style="text-align:right">${v}</div></div>`;
  }).join('');
}
const ROLE_COLORS={db:'p',k8s:'',hadoop:'a',cache:'o',web:'g',monitoring:'',app:'',paas:'t',middleware:'a'};
// per-application rollup from /api/aggregations.by_app — rendered as a searchable
// table; clicking a row drills into the Inventory tab filtered to that app.
let _dashByApp = [];
function renderDashApp(filter){
  filter = (filter||'').toLowerCase().trim();
  const rows = _dashByApp.filter(a => !filter
    || (a.app_id||'').toLowerCase().includes(filter)
    || (a.name||'').toLowerCase().includes(filter));
  const shown = rows.slice(0, 50);
  $('dashAppBody').innerHTML = shown.map(a=>`<tr class="clickable" onclick="drillApp('${attr(a.app_id)}')" title="open Inventory filtered to ${attr(a.app_id)}">
    <td style="text-align:left"><b>${esc(a.app_id)}</b>${a.name&&a.name!==a.app_id?`<br><span class="muted" style="font-size:11px">${esc(a.name)}</span>`:''}</td>
    <td>${esc(a.env||'–')}</td><td>${esc(a.strategy||'–')}</td><td>${esc(a.target||'–')}</td>
    <td style="text-align:right">${a.servers}</td>
    <td style="text-align:right">${(a.cores||0).toLocaleString()}</td>
    <td style="text-align:right">${(a.mem_gb||0).toLocaleString()}</td></tr>`).join('')
    || '<tr><td colspan="7" class="muted">no apps match</td></tr>';
  $('dashAppCount').textContent = `${rows.length} app${rows.length!==1?'s':''}${filter?' matched':''}${rows.length>shown.length?` · showing ${shown.length}`:''}`;
}
function drillApp(appId){
  state.filters = {app_id: appId}; state.q=''; state.page=1; state.selected=new Set();
  document.querySelector('.tab[data-tab=inventory]').click();
}
async function loadDashboard(){
  let a; try{ a = await api('/aggregations'); }catch(e){ return; }
  const tiles = [
    {k:'servers', v:a.servers},
    {k:'total cores', v:a.cores.toLocaleString()},
    {k:'total RAM', v:a.mem_gb.toLocaleString(), sub:'GB'},
    {k:'waves', v:a.waves.length},
    {k:'targets mapped', v:Object.values(a.by_target).reduce((s,x)=>s+x,0)},
    {k:'high-utilization', v:a.high_utilization, warn:true, sub:'≥80%'},
    {k:'low-confidence', v:(a.confidence['low(<0.7)']||0)+(a.confidence['medium(0.7-0.85)']||0), warn:true, sub:'<0.85'},
  ];
  $('dashTiles').innerHTML = tiles.map(t=>`<div class="tile ${t.warn?'warn':''}"><div class="k">${t.k}</div><div class="v">${t.v}${t.sub?` <small>${t.sub}</small>`:''}</div></div>`).join('');
  $('barRole').innerHTML = bars(a.by_role, ROLE_COLORS);
  $('barTarget').innerHTML = bars(a.by_target, {CDB:'p',EMR:'a',TKE:'',CVM:''});
  $('barOs').innerHTML = bars(a.by_os);
  $('barConf').innerHTML = bars(a.confidence, {'high(>=0.85)':'g','medium(0.7-0.85)':'a','low(<0.7)':'r'});
  $('barRegion').innerHTML = bars(a.by_region);
  _dashByApp = a.by_app || [];
  renderDashApp($('dashAppSearch') ? $('dashAppSearch').value : '');
  $('dashWaves').innerHTML = a.waves.map(w=>`<div class="wave" onclick="document.querySelector('.tab[data-tab=waves]').click(); openWave('${w.id}')">
    <div class="row"><span class="name">${esc(w.name)}</span><span class="pill ${w.stage.startsWith('1')?'high':w.stage.startsWith('4')?'medium':'low'}">${esc(w.stage)}</span><span style="margin-left:auto" class="meta">${w.n} servers</span></div></div>`).join('');
  // data-gap headline (best-effort — the Data Quality tab has the full report)
  loadDashDataGap();
}

async function loadDashDataGap(){
  const out=$('dashDataGapBody');
  try{
    const g = await api('/data-gaps');
    const oe=g.os_eol||{}, wa=g.warranty||{};
    const tile=(label,n,color)=>`<div class="tile ${n?'warn':''}" style="min-width:150px;cursor:pointer" onclick="document.querySelector('.tab[data-tab=data-quality]').click()">
      <div class="k">${esc(label)}</div><div class="v" style="color:${color}">${n}</div></div>`;
    out.innerHTML =
      tile('OS EOL (expired)', oe.expired||0, 'var(--red)') +
      tile('OS EOL (expiring)', oe.expiring||0, 'var(--amber)') +
      tile('warranty unknown', wa.unknown||0, 'var(--amber)') +
      tile('warranty expired', wa.expired||0, 'var(--red)') +
      tile('no util telemetry', g.missing_utilization||0, 'var(--amber)') +
      tile('no code profile', g.missing_code_profile||0, 'var(--amber)');
  }catch(e){ out.innerHTML='<span class="muted">data gaps unavailable</span>'; }
}
