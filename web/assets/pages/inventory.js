/* ---------- inventory ---------- */
function debounceSearch(){ clearTimeout(searchTimer); state.page=1; state.q=$('q').value.trim();
  searchTimer=setTimeout(fetchInv, 250); }
function resetFilters(){ state.filters={}; state.q=''; state.page=1; $('q').value=''; fetchInv(); }
function applyQuick(kind){
  state.filters={};
  if(kind==='highutil') state.filters.util_mem_min=80;
  if(kind==='lowconf') state.filters.conf_max=0.79;
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
    ['role','Role',fc.role,{db:'p',k8s:'',hadoop:'a',cache:'o',web:'g',monitoring:'',app:''}],
    ['env','Env',fc.env,{prod:'r',staging:'a',dev:'g'}],
    ['os','OS',fc.os],
    ['source_type','Type',fc.source_type],
    ['criticality','Criticality',fc.criticality,{high:'r',medium:'a',low:'g'}],
    ['target_product','Target',fc.target_product,{CDB:'p',EMR:'a',TKE:'',CVM:''}],
  ];
  $('facets').innerHTML = groups.map(([dim,label,data,colors])=>{
    if(!data || !Object.keys(data).length) return '';
    const rows = Object.entries(data).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
      const active = state.filters[dim]===k ? 'active' : '';
      return `<div class="facet ${active}" onclick="toggleFacet('${dim}','${esc(k)}')"><span>${esc(k)}</span><span class="n">${v}</span></div>`;
    }).join('');
    return `<div class="facet-group"><div class="ft">${label}</div>${rows}</div>`;
  }).join('') || '<span class="muted">no facets</span>';
  // active chips
  const chips = [];
  if(state.q) chips.push(`search:${state.q}`);
  for(const[k,v]of Object.entries(state.filters)) chips.push(`${k}=${v}`);
  $('activeChips').innerHTML = chips.map(c=>`<span class="chip">${esc(c)}<span class="x" onclick="resetFilters()">×</span></span>`).join('');
  // table
  const cols = [['hostname','Hostname'],['role','Role'],['source_type','Type'],['os','OS'],['cpu_cores','CPU'],['mem_gb','Mem'],['env','Env'],['business_criticality','Crit'],['','Target'],['','Util%'],['confidence','Conf']];
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
      <td data-label="Role"><span class="tag ${s.role}">${s.role||'-'}</span></td>
      <td data-label="Type">${s.source_type||'-'}</td>
      <td data-label="OS">${s.os||'-'}</td>
      <td data-label="CPU">${s.cpu_cores}</td>
      <td data-label="Mem">${s.mem_gb}G</td>
      <td data-label="Env">${s.env||'-'}</td>
      <td data-label="Crit"><span class="pill ${s.business_criticality}">${s.business_criticality||'-'}</span></td>
      <td data-label="Target"><b>${m.target?m.target.product:'-'}</b> <span class="muted">${m.target?esc(m.target.spec).slice(0,18):''}</span></td>
      <td data-label="Util%" class="conf">${fmtUtil(s.utilization)}</td>
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
                'target_product','target_spec','confidence'];
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
      target_product:t.product||'', target_spec:t.spec||'', confidence:m.confidence??''};
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
