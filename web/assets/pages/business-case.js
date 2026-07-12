/* ---------- Business Case tab (F2 — TCO / portfolio rollup) ---------- */
function _bcTile(label, val, sub){
  return `<div class="tile"><div class="tlabel">${esc(label)}</div><div class="tval">${esc(val)}</div><div class="tsub muted">${esc(sub||'')}</div></div>`;
}
function _barsFrom(dict, fmt){
  // dict {key: number} -> a simple horizontal bar list (max-normalized)
  const entries = Object.entries(dict||{}).sort((a,b)=>b[1]-a[1]);
  if(!entries.length) return '<span class="muted">-</span>';
  const max = entries[0][1] || 1;
  return entries.map(([k,v])=>{
    const pct = Math.max(2, (v/max)*100);
    return `<div class="bar-row"><span class="bar-label">${esc(k)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <span class="bar-val">${fmt(v)}</span></div>`;
  }).join('');
}
function _renderBusinessCase(bc){
  if(!bc){ $('bcTiles').innerHTML = '<span class="muted">no business case yet — click Refresh</span>'; return; }
  const sav = bc.annual_savings==null ? 'n/a (no on-prem rate)' : `$${bc.annual_savings.toLocaleString()}`;
  $('bcTiles').innerHTML = [
    _bcTile('cloud yearly', `$${bc.cloud_yearly.toLocaleString()}`, `monthly $${bc.cloud_monthly.toLocaleString()}`),
    _bcTile('priced servers', `${bc.priced_servers}/${bc.priced_servers+bc.unpriced_servers}`, `unpriced ${bc.unpriced_servers}`),
    _bcTile('annual savings', sav, 'on-prem vs cloud'),
    _bcTile('pricing source', bc.pricing_source, bc.snapshot_id?('snapshot '+bc.snapshot_id):''),
  ].join('');
  // bars: strategy + product (yearly $)
  const stratYearly = Object.fromEntries(Object.entries(bc.per_strategy||{}).map(([k,v])=>[k, v.yearly]));
  $('bcStrategy').innerHTML = _barsFrom(stratYearly, v=>`$${v.toLocaleString()}`);
  $('bcProduct').innerHTML = _barsFrom(bc.per_product, v=>`$${v.toLocaleString()}`);
  // per-server table (top 50)
  const rows = (bc.per_server||[]).slice().sort((a,b)=>b.yearly-a.yearly).slice(0,50);
  $('bcTbl').querySelector('tbody').innerHTML = rows.map(r=>
    `<tr><td>${esc(r.hostname)}</td><td>${esc((r.app_ids||[]).join(',')||'-')}</td>
     <td>${esc(r.product)}</td><td>${esc(r.region)}</td><td>${esc(r.strategy)}</td>
     <td>${r.monthly.toLocaleString()}</td><td>${r.yearly.toLocaleString()}</td>
     <td>${esc(r.basis)}</td></tr>`).join('') || '<tr><td class="muted" colspan=8>run Rebuild first</td></tr>';
}
async function loadBusinessCase(save){
  try{
    $('bcTiles').innerHTML = '<span class="spinner"></span> computing business case…';
    const bc = await api('/business-case',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({save:save!==false})});
    _renderBusinessCase(bc);
    toast('business case '+(save!==false?'saved':'computed'), 'ok');
  }catch(e){ $('bcTiles').innerHTML = '<span class="ev-err">'+esc(String(e))+'</span>'; }
}
async function loadLatestBusinessCase(){
  try{
    const bc = await api('/business-case');
    // the snapshot payload is nested under 'payload'
    _renderBusinessCase(bc.payload||bc);
  }catch(e){
    // no saved snapshot yet — compute one without saving (the Refresh button saves)
    loadBusinessCase(false);
  }
}
function loadBusinessCaseInit(){ loadLatestBusinessCase(); }