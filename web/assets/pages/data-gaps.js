/* ---------- Data Quality tab (data-gap) ---------- */
/* Two cards: (1) portfolio data-gaps report (confidence / missing fields /
 * warranty / thinly-known hosts) from /api/data-gaps, and (2) the shadow-IT
 * discovery diff (unknown_hosts / cmdb_orphans / drifted) from
 * /api/discovery/diff (+ POST /api/discovery/scan to refresh). */
const _DG_CONF_COLOR = {'high(>=0.85)':'var(--green)','medium(0.5-0.85)':'var(--amber)','low(<0.5)':'var(--red)','none':'#888'};
const _DG_WAR_COLOR = {active:'var(--green)',expiring:'var(--amber)',expired:'var(--red)',unknown:'#888'};

async function loadDataGaps(){
  const out=$('dataGaps');
  out.innerHTML='<span class="spinner"></span> loading data gaps…';
  try{
    const r = await api('/data-gaps');
    const conf = r.confidence||{};
    const missing = r.missing||{};
    const war = r.warranty||{};
    const oseol = r.os_eol||{};
    const confRows = Object.keys(_DG_CONF_COLOR).map(k=>{
      const c=_DG_CONF_COLOR[k]; const n=conf[k]||0;
      return `<tr><td><span style="color:${c}">${esc(k)}</span></td><td>${n}</td></tr>`;
    }).join('');
    const missRows = Object.entries(missing).sort((a,b)=>b[1]-a[1]).map(([f,n])=>
      `<tr><td>${esc(f)}</td><td>${n}</td><td style="color:${n? 'var(--amber)':'var(--green)'}">${n? 'gap':'ok'}</td></tr>`).join('');
    const warRows = Object.keys(_DG_WAR_COLOR).map(k=>{
      const c=_DG_WAR_COLOR[k]; const n=war[k]||0;
      return `<tr><td><span style="color:${c}">${esc(k)}</span></td><td>${n}</td></tr>`;
    }).join('');
    const osRows = Object.keys(_DG_WAR_COLOR).map(k=>{
      const c=_DG_WAR_COLOR[k]; const n=oseol[k]||0;
      return `<tr><td><span style="color:${c}">${esc(k)}</span></td><td>${n}</td></tr>`;
    }).join('');
    const worst = (r.worst_hosts||[]).map(h=>{
      const wc=_DG_WAR_COLOR[h.warranty]||'#888';
      const oc=_DG_WAR_COLOR[h.os_eol]||'#888';
      const conf = h.confidence==null?'-':(h.confidence).toFixed(2);
      const ccol = h.confidence==null?'#888':(h.confidence>=0.85?'var(--green)':(h.confidence>=0.5?'var(--amber)':'var(--red)'));
      return `<tr><td>${esc(h.hostname)}</td><td style="color:${ccol}">${conf}</td>
        <td>${esc((h.missing||[]).join(', ')||'-')}</td>
        <td>${h.has_util?'<span style="color:var(--green)">yes</span>':'<span style="color:var(--red)">no</span>'}</td>
        <td>${h.has_profile?'<span style="color:var(--green)">yes</span>':'<span style="color:var(--red)">no</span>'}</td>
        <td style="color:${wc}">${esc(h.warranty)}</td>
        <td style="color:${oc}">${esc(h.os_eol)}</td></tr>`;
    }).join('');
    out.innerHTML = `
      <div class="muted" style="margin-bottom:6px">${esc(r.summary||'')}</div>
      <div class="xscroll"><table class="tbl mcard" style="margin-bottom:10px">
        <thead><tr><th>assessment confidence</th><th>hosts</th></tr></thead><tbody>${confRows}</tbody></table></div>
      <div class="row" style="gap:14px;flex-wrap:wrap;align-items:flex-start">
        <div><div class="muted" style="font-size:12px;margin-bottom:2px">missing characterization fields</div>
          <table class="tbl mcard"><thead><tr><th>field</th><th>hosts</th><th></th></tr></thead><tbody>${missRows}</tbody></table></div>
        <div><div class="muted" style="font-size:12px;margin-bottom:2px">warranty distribution</div>
          <table class="tbl mcard"><thead><tr><th>bucket</th><th>hosts</th></tr></thead><tbody>${warRows}</tbody></table></div>
        <div><div class="muted" style="font-size:12px;margin-bottom:2px">OS vendor-support (EOL) distribution</div>
          <table class="tbl mcard"><thead><tr><th>bucket</th><th>hosts</th></tr></thead><tbody>${osRows}</tbody></table></div>
      </div>
      <div class="muted" style="font-size:12px;margin:10px 0 2px">thinly-known hosts (lowest confidence first — run discovery / ingest telemetry / profile the app)</div>
      <div class="xscroll"><table class="tbl mcard"><thead><tr><th>hostname</th><th>conf</th><th>missing</th><th>util</th><th>profile</th><th>warranty</th><th>os_eol</th></tr></thead><tbody>${worst||'<tr><td class="muted">none</td></tr>'}</tbody></table></div>`;
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
}

/* ---------- Shadow IT discovery diff (Gap1) ---------- */
function _discoveryRow(h){
  return `<tr><td>${esc(h.hostname||h.fqdn||'-')}</td><td>${esc((h.ips||[]).join(', ')||'-')}</td>
    <td>${esc(h.source||'-')}</td><td>${esc(h.role||'-')}</td>
    <td>${esc(h.reason||'-')}</td></tr>`;
}
async function loadDiscovery(){
  const out=$('discoveryDiff');
  out.innerHTML='<span class="spinner"></span> loading shadow-IT diff…';
  try{
    const r = await fetch(API+'/discovery/diff');
    // no scan run yet -> friendly placeholder, not a red error (this is the
    // normal first-run state, not a failure)
    if(r.status===404){
      out.innerHTML='<span class="muted">no scan run yet — click “Run discovery scan” (needs <code>IDC_DISCOVERY_PATH</code> pointing at a network/vCenter snapshot)</span>';
      return;
    }
    if(!r.ok) throw new Error(await r.text());
    const diff = await r.json();
    const u=(diff.unknown_hosts||[]).map(_discoveryRow).join('')||'<tr><td class="muted">none — every discovered host is in CMDB</td></tr>';
    const o=(diff.cmdb_orphans||[]).map(_discoveryRow).join('')||'<tr><td class="muted">none</td></tr>';
    const d=(diff.drifted||[]).map(x=>`<tr><td>${esc(x.hostname||'-')}</td><td>${esc(x.field)}</td><td>${esc(x.cmdb)}</td><td>${esc(x.discovered)}</td></tr>`).join('')||'<tr><td class="muted">none</td></tr>';
    const srcNote = diff.source==='off' ? ' <span class="muted">(discovery off — set IDC_DISCOVERY_PATH)</span>' : '';
    out.innerHTML = `
      <div class="muted" style="margin-bottom:6px">${esc(diff.summary||'')} · scanned ${esc(diff.scanned_at||'-')} · source ${esc(diff.source||'-')}${srcNote}</div>
      <h4 style="color:var(--red)">unknown hosts (in network/vCenter, NOT in CMDB — shadow IT)</h4>
      <div class="xscroll"><table class="tbl mcard"><thead><tr><th>hostname</th><th>ips</th><th>source</th><th>role</th><th>reason</th></tr></thead><tbody>${u}</tbody></table></div>
      <h4 style="color:var(--amber);margin-top:10px">CMDB orphans (in CMDB, not seen on network — zombie / retired candidate)</h4>
      <div class="xscroll"><table class="tbl mcard"><thead><tr><th>hostname</th><th>ips</th><th>source</th><th>role</th><th>reason</th></tr></thead><tbody>${o}</tbody></table></div>
      <h4 style="color:#a06bff;margin-top:10px">drifted (in both, attributes differ)</h4>
      <div class="xscroll"><table class="tbl mcard"><thead><tr><th>hostname</th><th>field</th><th>cmdb</th><th>discovered</th></tr></thead><tbody>${d}</tbody></table></div>`;
  }catch(e){ out.innerHTML='<span class="ev-err">Error: '+esc(String(e))+'</span>'; }
}
async function runDiscoveryScan(){
  const out=$('discoveryDiff');
  out.innerHTML='<span class="spinner"></span> running discovery scan…';
  try{
    const r = await api('/discovery/scan',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({})});
    toast(`scan done: ${(r.unknown_hosts||[]).length} unknown / ${(r.cmdb_orphans||[]).length} orphans`, 'ok');
    loadDiscovery();
  }catch(e){ out.innerHTML='<span class="ev-err">Scan failed: '+esc(String(e))+'</span>'; }
}