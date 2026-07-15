/* ---------- Business Case tab (F2 — TCO / portfolio rollup) ---------- */
/* Expandable per-server ledger: each of the top-50 rows opens an inline detail
   panel with the full cost story (breakdown, why-this-cost, right-size
   opportunity, risk signals, actions). The detail is fetched lazily from
   /api/servers/{id} on first expand and memoized in _bcDetailCache so re-opening
   is instant. The on-prem side uses the portfolio's onprem_rate (exposed by the
   backend) so the per-host annual saving is computed, not fabricated. */
const _bcDetailCache = {};     // server_id -> full /api/servers/{id} record
let _bcRows = [];              // the top-50 per_server rows currently rendered
let _bcMeta = {onprem_rate:0, pricing_source:''};

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
function _bcMoney(n){ return '$'+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0}); }

function _renderBusinessCase(bc){
  if(!bc){ $('bcTiles').innerHTML = '<span class="muted">no business case yet — click Refresh</span>'; return; }
  _bcMeta = {onprem_rate: bc.onprem_rate||0, pricing_source: bc.pricing_source||''};
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
  // per-server table (top 50) — click a row to expand its cost detail
  _bcRows = (bc.per_server||[]).slice().sort((a,b)=>b.yearly-a.yearly).slice(0,50);
  const tb = $('bcTbl').querySelector('tbody');
  tb.innerHTML = _bcRows.map((r,i)=>
    `<tr data-bcrow="${i}" onclick="bcToggleRow(${i})">
       <td class="chev"><span class="bc-chev">▸</span></td>
       <td><b>${esc(r.hostname)}</b></td>
       <td>${esc((r.app_ids||[]).join(',')||'-')}</td>
       <td>${esc(r.product)}</td>
       <td>${esc(r.region)}</td>
       <td>${_stratBadge(r.strategy)}</td>
       <td>${r.monthly.toLocaleString()}</td>
       <td>${r.yearly.toLocaleString()}</td>
       <td>${esc(r.basis)}</td>
     </tr>
     <tr class="bc-detail hidden" id="bcd-${i}"><td colspan="9"></td></tr>`
  ).join('') || '<tr><td class="muted" colspan=9>run Rebuild first</td></tr>';
}

/* toggle a row's inline detail. Lazy-fetch + cache the full server record on
   first open; thereafter just show/hide. The cost breakdown always renders from
   the row itself (it's in the snapshot); the live record only augments the
   "why / right-size / risk" sections, so a 404 (stale snapshot id) degrades to
   a row-only panel + a refresh hint instead of a cryptic error. */
function bcToggleRow(i){
  const row = document.querySelector(`tr[data-bcrow="${i}"]`);
  const det = $('bcd-'+i);
  if(!row || !det) return;
  const opening = det.classList.contains('hidden');
  det.classList.toggle('hidden');
  row.classList.toggle('open', opening);
  if(!opening) return;             // closing — nothing to render
  const r = _bcRows[i];
  if(!r) return;
  const cell = det.firstElementChild;
  if(_bcDetailCache[r.server_id]){
    cell.innerHTML = _bcDetailHTML(r, _bcDetailCache[r.server_id]);
    return;
  }
  cell.innerHTML = '<span class="spinner"></span> loading cost detail…';
  fetch(API+'/servers/'+encodeURIComponent(r.server_id))
    .then(async res=> res.ok ? res.json() : {__missing: res.status})
    .then(s=>{
      if(s && s.__missing){           // server id not in the current estate
        if(!det.classList.contains('hidden')) cell.innerHTML = _bcDetailHTML(r, null);
        return;
      }
      _bcDetailCache[r.server_id] = s;
      // re-render only if the row is still open (user may have closed it)
      if(!det.classList.contains('hidden')) cell.innerHTML = _bcDetailHTML(r, s);
    })
    .catch(()=>{ if(!det.classList.contains('hidden')) cell.innerHTML = _bcDetailHTML(r, null); });
}

/* the cost-focused detail panel — the value the flat table can't show.
   ``s`` is the full /api/servers/{id} record, or null when that record is
   unavailable (stale snapshot id). The cost breakdown + target come from the
   row itself; the live record only adds confidence / rationale / utilization
   / warranty / OS-EOL + enables the what-if + open-record actions. */
function _bcDetailHTML(row, s){
  const have = !!s;
  const m = (s && s.match) || {};
  const u = (s && s.utilization) || {};
  const rate = _bcMeta.onprem_rate || 0;
  const migrating = !['retain','retire'].includes(row.strategy);
  const onpremY = (migrating && rate>0) ? rate*12 + (row.eol_premium_yearly||0) : null;
  const saving = onpremY!=null ? onpremY - row.yearly : null;
  const kv = (l,v)=>`<div class="kv-row"><span class="kv-label">${esc(l)}</span><span class="kv-val">${v}</span></div>`;
  const dash = '<span class="muted">—</span>';
  // target is in the row itself (product/spec/region); confidence + rule
  // rationale need the live match.
  const target = `${esc(row.product||'-')} <span class="muted">${esc(row.spec||'')}</span> @ ${esc(row.region||'-')}`;
  const conf = (have && m.confidence!=null) ? `match conf <b>${m.confidence.toFixed(2)}</b>` : `match conf ${dash}`;
  const rule = (have && m.rationale) ? esc(m.rationale) : dash;

  // --- right-size opportunity: utilization vs allocated (needs live record) ---
  const cpuU = u.cpu_p95, memU = u.mem_p95, diskU = u.disk_used_pct;
  const lowUtil = have && ((cpuU!=null && cpuU < 30) || (memU!=null && memU < 30));
  const utilTxt = !have ? dash
    : (cpuU==null && memU==null && diskU==null) ? '-'
    : `cpu ${cpuU??'-'}% · mem ${memU??'-'}% · disk ${diskU??'-'}% <span class="muted">(${esc(u.source||'-')})</span>`;
  const allocTxt = !have ? dash : `${s.cpu_cores||'-'} vCPU / ${s.mem_gb||'-'} GB`;
  const rsFlag = !have ? `<div class="muted" style="margin-top:4px">live utilization unavailable (stale snapshot).</div>`
    : lowUtil ? `<div style="color:var(--amber);margin-top:4px">⚠ low utilization — right-sizing may save. Try:</div>`
    : `<div class="muted" style="margin-top:4px">utilization looks healthy; right-sizing unlikely to shrink the target.</div>`;
  const rsActions = have
    ? `<div class="bc-actions">
        <select id="bcRsSel-${attr(row.server_id)}" class="sm" aria-label="right-size strategy">
          <option value="as_is">as-is</option>
          <option value="measured" selected>measured</option>
          <option value="right_size">right-size</option>
        </select>
        <button class="sm primary" onclick="bcRightSize('${attr(row.server_id)}')">run what-if</button>
      </div>` : '';

  // --- risk signals (drive the on-prem premium + readiness) ---
  const risk = have ? `${_warrantyBadge(s)} &nbsp; OS-EOL ${_osEolBadge(s)}` : dash;

  // --- on-prem vs cloud + per-host saving (only when we know the rate) ---
  const onpremBlock = onpremY!=null
    ? kv('on-prem yearly', `${_bcMoney(onpremY)} <span class="muted">(${_bcMoney(rate)}/mo${row.eol_premium_yearly?` + ${_bcMoney(row.eol_premium_yearly)} EOL premium`:''})</span>`)
      + kv('annual saving', `<b style="color:${saving>=0?'var(--green)':'var(--red)'}">${saving>=0?'+':''}${_bcMoney(saving)}</b>`)
    : kv('on-prem / saving', '<span class="muted">n/a — no on-prem rate configured</span>');
  const stratNote = migrating ? '' : `<div class="muted" style="margin-top:4px">not migrating (${_stratBadge(row.strategy)}) — excluded from cloud cost + on-prem baseline.</div>`;
  const staleBanner = have ? '' : `<div class="bc-wi" style="color:var(--amber)">⚠ full server record unavailable — this row is from a saved snapshot whose server ids may be stale. Click <b>Refresh + save snapshot</b> to recompute from the current estate.</div>`;
  const openBtn = have
    ? `<button class="sm" onclick="openServer('${attr(row.server_id)}')">open full server record</button>
       <span class="muted" style="font-size:11px">7R strategy · audit target · explain match · code scan</span>`
    : '<span class="muted" style="font-size:11px">refresh the snapshot to enable the full record + what-if.</span>';

  return `<div class="bc-panel">
    <div>
      <h4>Cost breakdown</h4>
      <div class="kv">
        ${kv('monthly', _bcMoney(row.monthly))}
        ${kv('yearly', _bcMoney(row.yearly))}
        ${kv('pricing basis', `${esc(row.basis)} <span class="muted">· ${esc(_bcMeta.pricing_source||'-')}</span>`)}
        ${onpremBlock}
      </div>
      ${stratNote}
    </div>
    <div>
      <h4>Why this cost</h4>
      <div class="kv">
        ${kv('target', target)}
        ${kv('confidence', conf)}
        ${kv('sizing basis', esc((have?s.sizing_basis:'')||row.basis||'-'))}
        ${kv('rule', rule)}
      </div>
    </div>
    <div>
      <h4>Right-size opportunity</h4>
      <div class="kv">
        ${kv('utilization (p95)', utilTxt)}
        ${kv('allocated', allocTxt)}
      </div>
      ${rsFlag}
      ${rsActions}
    </div>
    <div>
      <h4>Risk signals</h4>
      <div class="kv">${kv('warranty / OS-EOL', risk)}</div>
      <div class="muted" style="font-size:11px;margin-top:4px">out-of-warranty / EOL hosts carry an on-prem extended-support premium and lower readiness.</div>
    </div>
    ${staleBanner}
    <div class="bc-wi" id="bcWiOut-${attr(row.server_id)}"></div>
    <div class="bc-actions">
      ${openBtn}
    </div>
  </div>`;
}

/* inline right-size what-if for one host — calls /api/what-if/right-size and
   renders before -> after -> delta in the panel's result area. */
async function bcRightSize(serverId){
  const out = $('bcWiOut-'+serverId);
  const sel = $('bcRsSel-'+serverId);
  if(!out || !sel) return;
  out.innerHTML = '<span class="spinner"></span> re-pricing…';
  try{
    const r = await api('/what-if/right-size',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({server_id:serverId, strategy:sel.value})});
    const b = r.before||{}, a = r.after||{};
    const d = r.delta_yearly||0;
    const sign = d<0?'save':(d>0?'cost +':'no change');
    const col = d<0?'var(--green)':(d>0?'var(--red)':'var(--muted)');
    out.innerHTML = `<div class="mcard" style="padding:8px 10px">
      <div class="row" style="gap:14px;flex-wrap:wrap;align-items:baseline">
        <span><span class="muted">before</span> <b>${esc(b.product||'-')}</b> ${esc(b.spec||'')} <span class="muted">@ ${esc(b.region||'-')}</span> → <b>${_bcMoney(b.yearly)}</b>/yr</span>
        <span><span class="muted">after</span> <b>${esc(a.product||'-')}</b> ${esc(a.spec||'')} <span class="muted">@ ${esc(a.region||'-')}</span> → <b>${_bcMoney(a.yearly)}</b>/yr</span>
        <span><span class="muted">delta</span> <b style="color:${col}">${d>=0?'+':''}${_bcMoney(d)}/yr</b> <span class="muted">(${sign})</span></span>
      </div>
      ${r.rationale?`<div class="muted" style="font-size:11px;margin-top:4px">${esc(r.rationale)}</div>`:''}
    </div>`;
  }catch(e){ out.innerHTML = '<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
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