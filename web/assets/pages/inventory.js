/* ---------- inventory ---------- */
function debounceSearch(){ clearTimeout(searchTimer); state.page=1; state.q=$('q').value.trim();
  searchTimer=setTimeout(fetchInv, 250); }
function resetFilters(){ state.filters={}; state.q=''; state.page=1; $('q').value=''; fetchInv(); }
function applyQuick(kind){
  state.filters={};
  if(kind==='highutil') state.filters.util_mem_min=80;
  if(kind==='lowconf') state.filters.conf_max=0.79;
  if(kind==='eolos') state.filters.os_eol_bucket='expired';     // data-gap: EOL OS cohort
  if(kind==='oosw') state.filters.warranty_bucket='expired';    // data-gap: 脱保 cohort
  state.page=1; fetchInv();
}
function toggleFacet(dim, val){
  if(state.filters[dim]===val) delete state.filters[dim];
  else state.filters[dim]=val;
  state.page=1; fetchInv();
}
function params(extra={}){ const p=new URLSearchParams(); p.set('page',state.page); p.set('page_size',state.page_size);
  p.set('order_by',state.order_by); p.set('order_dir',state.order_dir);
  if(state.q) p.set('q',state.q);
  for(const[k,v]of Object.entries(state.filters)) p.set(k,v);
  for(const[k,v]of Object.entries(extra)) if(v!=null) p.set(k,v);
  return p.toString(); }
async function fetchInv(){
  let r; try{ r = await api('/servers?'+params()); }catch(e){ return; }
  state.last = r; renderInv(r);
}
function renderInv(r){
  // facets
  const fc = r.facets || {};
  const groups = [
    ['role','Role',fc.role,{db:'p',k8s:'',hadoop:'a',cache:'o',web:'g',monitoring:'',app:'',paas:'t',middleware:'a'}],
    ['env','Env',fc.env,{prod:'r',staging:'a',dev:'g'}],
    ['os','OS',fc.os],
    ['source_type','Type',fc.source_type],
    ['criticality','Criticality',fc.criticality,{high:'r',medium:'a',low:'g'}],
    ['target_product','Target',fc.target_product,{CDB:'p',EMR:'a',TKE:'',CVM:'',TData:'t'}],
    ['os_eol_bucket','OS EOL',fc.os_eol_bucket,{active:'g',expiring:'a',expired:'r',unknown:'',unknown_or_none:''}],
    ['warranty_bucket','Warranty',fc.warranty_bucket,{active:'g',expiring:'a',expired:'r',unknown:''}],
  ];
  $('facets').innerHTML = groups.map(([dim,label,data,colors])=>{
    if(!data || !Object.keys(data).length) return '';
    const rows = Object.entries(data).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
      const active = state.filters[dim]===k ? 'active' : '';
      return `<div class="facet ${active}" onclick="toggleFacet('${dim}','${attr(k)}')"><span>${esc(k)}</span><span class="n">${v}</span></div>`;
    }).join('');
    return `<div class="facet-group"><div class="ft">${label}</div>${rows}</div>`;
  }).join('') || '<span class="muted">no facets</span>';
  // active chips
  const chips = [];
  if(state.q) chips.push(`search:${state.q}`);
  for(const[k,v]of Object.entries(state.filters)) chips.push(`${k}=${v}`);
  $('activeChips').innerHTML = chips.map(c=>`<span class="chip">${esc(c)}<span class="x" onclick="resetFilters()">×</span></span>`).join('');
  // table
  const cols = [['hostname','Hostname'],['role','Role'],['source_type','Type'],['os','OS'],['cpu_cores','CPU'],['mem_gb','Mem'],['env','Env'],['business_criticality','Crit'],['','7R'],['','Target'],['','Util%'],['','Support'],['confidence','Conf']];
  const thead = '<tr>'+cols.map(([k,l])=>{
    if(!k) return `<th>${l}</th>`;
    const s = state.order_by===k ? 'sorted' : '';
    const arr = state.order_by===k ? (state.order_dir==='asc'?' ▲':' ▼') : '';
    return `<th class="${s}" onclick="sortBy('${k}')">${l}${arr}</th>`;
  }).join('')+'</tr>';
  const tbody = (r.items||[]).map(s=>{
    const m=s.match||{};
    return `<tr onclick="openServer('${s.id}')">
      <td data-label="Hostname"><input type="checkbox" onclick="event.stopPropagation();toggleSel('${s.id}',this.checked)" ${state.selected.has(s.id)?'checked':''} style="vertical-align:middle;margin-right:4px"/><b>${esc(s.hostname)}</b></td>
      <td data-label="Role"><span class="tag ${esc(s.role||'')}">${esc(s.role||'-')}</span></td>
      <td data-label="Type">${esc(s.source_type||'-')}</td>
      <td data-label="OS">${esc(s.os||'-')}</td>
      <td data-label="CPU">${s.cpu_cores}</td>
      <td data-label="Mem">${s.mem_gb}G</td>
      <td data-label="Env">${esc(s.env||'-')}</td>
      <td data-label="Crit"><span class="pill ${esc(s.business_criticality||'')}">${esc(s.business_criticality||'-')}</span></td>
      <td data-label="7R">${_stratBadge(s.seven_r)}</td>
      <td data-label="Target"><b>${m.target?esc(m.target.product):'-'}</b> <span class="muted">${m.target?esc(m.target.spec).slice(0,18):''}</span></td>
      <td data-label="Util%" class="conf">${fmtUtil(s.utilization)}</td>
      <td data-label="Support" class="conf">${bucketBadge(s.warranty_bucket,'warranty')} ${bucketBadge(s.os_eol_bucket,'OS EOL')}</td>
      <td data-label="Conf" class="conf">${m.confidence?'_'+m.confidence.toFixed(1):'-'}</td>
    </tr>`;}).join('');
  $('inv').innerHTML = `<thead>${thead}</thead><tbody>${tbody}</tbody>`;
  // pager
  const pages = Math.max(1, Math.ceil(r.total/state.page_size));
  $('pageInfo').textContent = `page ${r.page} / ${pages}`;
  $('totalInfo').textContent = `${r.total} servers`;
  $('selInfo').textContent = state.selected.size ? `${state.selected.size} selected` : '';
}
function sortBy(k){ if(state.order_by===k) state.order_dir = state.order_dir==='asc'?'desc':'asc'; else { state.order_by=k; state.order_dir='asc'; } fetchInv(); }
function goPage(d){ const p=Math.max(1,state.page+d); state.page=p; fetchInv(); }
function toggleSel(id,on){ if(on) state.selected.add(id); else state.selected.delete(id); $('selInfo').textContent = state.selected.size?`${state.selected.size} selected`:''; }
function selAllPage(on){ (state.last?.items||[]).forEach(s=>{ if(on) state.selected.add(s.id); else state.selected.delete(s.id); }); renderInv(state.last); }
function exportCsv(){ window.open('/api/servers.csv?'+params(), '_blank'); }
async function exportSelected(){
  if(!state.selected.size){ toast('no servers selected','warn'); return; }
  const ids=[...state.selected];
  if(ids.length > 1000){ if(!confirm(`${ids.length} servers selected — export first 1000?`)) return; }
  const cap = Math.min(ids.length, 1000);
  const cols = ['hostname','fqdn','ips','role','os','os_version','cpu_cores','mem_gb',
                'env','business_criticality','subnet','vlan','datacenter','app_ids',
                'seven_r','target_product','target_spec','confidence'];
  const cell = v => `"${String(v ?? '').replace(/"/g,'""')}"`;
  const lines = [cols.join(',')];
  for(let i=0;i<cap;i++){
    let s; try{ s = await (await fetch(API+'/servers/'+encodeURIComponent(ids[i]))).json(); }
    catch(e){ continue; }
    const m = s.match||{}, t = m.target||{};
    const row = {
      hostname:s.hostname, fqdn:s.fqdn, ips:(s.ips||[]).join(' '), role:s.role,
      os:s.os, os_version:s.os_version, cpu_cores:s.cpu_cores, mem_gb:s.mem_gb,
      env:s.env, business_criticality:s.business_criticality, subnet:s.subnet,
      vlan:s.vlan, datacenter:s.datacenter, app_ids:(s.app_ids||[]).join(' '),
      seven_r:s.seven_r||'', target_product:t.product||'', target_spec:t.spec||'',
      confidence:m.confidence??''};
    lines.push(cols.map(c=>cell(row[c])).join(','));
  }
  const blob = new Blob([lines.join('\n')], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'selected-servers.csv';
  a.click(); URL.revokeObjectURL(a.href);
}
function scopeAgentToFilter(){
  const p = params(); $('agPrompt').value = `Using the servers matching the current inventory filter (${p}), ` + ($('agPrompt').value || 'summarize what moves and the top risk.');
}

/* ---------- bulk per-host 7R analysis (AI / seven_r skill) over the filter ----------
   Processes the WHOLE filter in batches, committed per batch, RESUMABLE. The UI
   loops the backend: each /api/strategy/host/batch call processes one batch
   (batch_size hosts), commits it, and returns a cursor + more=true when hosts
   remain — the UI calls again with the cursor to continue. So a full-estate run
   (13,855 hosts × ~3s ≈ 11h) survives a network blip, a backend restart, or a
   tab close: committed batches are durable, and the cursor (kept in
   sessionStorage) lets the run resume from where it stopped instead of restarting.
   apply=true: retain/retire -> host disposition; migrating R -> clear override +
   fill the app's missing strategy. Off = dry-run preview. Same seven_r skill +
   precedence as the per-host Analyze-7R drawer button + the Inventory 7R column. */
let _invBatch7RAbort = null;
let _invBatch7RState = null;   // cumulative state across batches: {cursor,total,done,analyzed,...,rows,filter}
function bulkParams(){ const p=new URLSearchParams();
  if(state.q) p.set('q',state.q);
  for(const[k,v]of Object.entries(state.filters)) p.set(k,v);
  return p.toString(); }
function _invBatch7RRender(final){
  const s = _invBatch7RState; if(!s) return;
  const out = $('invBatch7ROut');
  const done = s.rows.length;   // cumulative hosts processed across all calls
  const spinner = final ? '' : ' <span class="spinner"></span>';
  const stopBtn = final ? '' : ' <button class="sm" onclick="runInvBatch7R()">stop</button>';
  const pct = s.total ? ` · ${Math.round(100*done/s.total)}%` : '';
  out.innerHTML =
    `<div class="muted" style="margin-bottom:4px">batch ${s.batches} · ${done}/${s.total||'?'} hosts${pct} · ${s.analyzed} ok${s.errors?`, ${s.errors} errors`:''}${s.apply?` · applied: ${s.appliedHost} retain/retire, ${s.appliedClear} cleared, ${s.appliedApp} app strategies`:''}${spinner}${stopBtn}</div>`
    + `<div style="max-height:300px;overflow:auto;border:1px solid var(--border);border-radius:6px">`
    + `<table class="mcard" style="width:100%"><thead><tr><th>host</th><th>current</th><th>recommended</th><th>applied</th></tr></thead><tbody>`
    + s.rows.slice(-200).map(r=>`<tr>
        <td><b>${esc(r.hostname||'-')}</b> <span class="muted" style="font-size:11px">${esc((r.app_ids||[]).join(',')||'-')}</span></td>
        <td>${_stratBadge(r.current_7r)}</td>
        <td>${r.ok?_stratBadge(r.strategy):'<span class="ev-err">'+esc(r.error||r.note||'failed')+'</span>'}</td>
        <td>${r.applied&&r.applied!=='none'?`<span style="color:var(--green)">${esc(r.applied)}</span>`:'<span class="muted">-</span>'}</td>
      </tr>`).join('')
    + `</tbody></table></div>`;
}
function toggleInvBatch7R(){
  const el = $('invBatch7R');
  const show = el.style.display === 'none';
  el.style.display = show ? '' : 'none';
  if(show){
    const setCount = n => $('invBatch7RCount').textContent =
      `${n} host${n===1?'':'s'} (current filter)`;
    if(state.last) setCount(state.last.total||0);
    else api('/servers?'+params()).then(r=>{ state.last=r; setCount(r.total||0); }).catch(()=>setCount('?'));
    // resume hint if a previous run was interrupted (cursor persisted per filter)
    const stored = sessionStorage.getItem('invBatch7R');
    if(stored){
      try{
        const v = JSON.parse(stored);
        if(v.filter === bulkParams() && v.cursor){
          $('invBatch7ROut').innerHTML =
            `<div class="mcard" style="padding:8px"><b>Resume?</b> <span class="muted">A previous run stopped at <b>${esc(v.cursor)}</b> — ${v.done}/${v.total||'?'} hosts done (committed + durable).</span>
               <button class="sm primary" onclick="runInvBatch7R(true)">resume from ${esc(v.cursor)}</button>
               <button class="sm" onclick="sessionStorage.removeItem('invBatch7R');toggleInvBatch7R();toggleInvBatch7R()">discard + start over</button></div>`;
        }
      }catch(e){}
    }
  }
}
async function runInvBatch7R(resume){
  if(_invBatch7RAbort){ _invBatch7RAbort.abort(); return; }   // running -> stop the loop
  const apply = $('invBatch7RApply').checked;
  const batchSize = Math.max(1, Math.min(500, parseInt($('invBatch7RLimit').value||'50',10)));
  const filter = bulkParams();
  let cursor = null;
  if(resume){
    try{ cursor = (JSON.parse(sessionStorage.getItem('invBatch7R')||'{}')).cursor || null; }catch(e){}
  }
  _invBatch7RState = {cursor, total:0, done:0, analyzed:0, errors:0,
                       appliedHost:0, appliedClear:0, appliedApp:0, batches:0,
                       rows:[], filter, apply};
  $('invBatch7RRun').textContent = 'stop';
  let more = true;
  while(more){
    more = await _runInvBatch7RCall(cursor, apply, batchSize);
    if(_invBatch7RAbort) break;   // user stopped
    cursor = _invBatch7RState.cursor;
    // persist the cursor so a reload/restart can resume; cleared when the run finishes
    sessionStorage.setItem('invBatch7R', JSON.stringify({filter:_invBatch7RState.filter, cursor, done:_invBatch7RState.rows.length, total:_invBatch7RState.total}));
  }
  _invBatch7RAbort = null;
  $('invBatch7RRun').textContent = 'Run';
  const s = _invBatch7RState;
  if(s.cursor == null) sessionStorage.removeItem('invBatch7R');   // finished -> drop the resume point
  _invBatch7RRender(true);
  if(apply && s.cursor == null){
    $('invBatch7ROut').insertAdjacentHTML('beforeend',
      `<div style="margin-top:6px"><button class="sm primary" onclick="fetchInv()">refresh inventory</button> <span class="muted" style="font-size:11px">to see the updated 7R column</span></div>`);
  }
}
async function _runInvBatch7RCall(cursor, apply, batchSize){
  // one POST = one batch; parse NDJSON, update _invBatch7RState, render live.
  // Returns true when more hosts remain (loop again), false when done/error.
  const s = _invBatch7RState;
  _invBatch7RAbort = new AbortController();
  let resp;
  try{
    resp = await fetch(API+'/strategy/host/batch?'+s.filter, {
      method:'POST', headers:{'content-type':'application/json'},
      body:JSON.stringify({apply, batch_size:batchSize, limit:0, cursor}),
      signal:_invBatch7RAbort.signal
    });
  }catch(e){ _invBatch7RAbort=null; return false; }
  if(!resp.ok){ _invBatch7RAbort=null; $('invBatch7ROut').insertAdjacentHTML('beforeend','<span class="ev-err">'+esc(await resp.text())+'</span>'); return false; }
  const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf='';
  let more = false;
  try{
    while(true){
      const {value, done:rd} = await reader.read();
      if(rd) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for(const line of lines){
        if(!line) continue;
        let j; try{ j = JSON.parse(line); }catch(e){ continue; }
        if(j.type==='start'){ s.total = j.total; }
        else if(j.type==='host'){
          s.rows.push(j);
          if(j.ok) s.analyzed++; else s.errors++;
          if(j.applied==='retain'||j.applied==='retire') s.appliedHost++;
          if(j.applied==='cleared') s.appliedClear++;
          _invBatch7RRender(false);
        }
        else if(j.type==='app'){ s.appliedApp++; }
        else if(j.type==='batch'){ s.batches++; }   // one committed batch checkpoint
        else if(j.type==='done'){
          s.cursor = j.cursor;   // null when the whole run is finished
          more = !!j.more;       // more hosts remain -> loop with the cursor
        }
      }
    }
  }catch(e){ /* abort / network — keep more=false so the loop stops; cursor retained in sessionStorage */ }
  _invBatch7RAbort = null;
  return more;
}
