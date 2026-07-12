/* idc-migrate core — shared globals/helpers, tab router, header stats, pipeline ops.
   Per-page logic lives in pages/*.js; main.js runs init. Loaded first. */
const API = location.origin + '/api';
let state = {q:'', filters:{}, page:1, page_size:50, order_by:'hostname', order_dir:'asc', last:null, selected:new Set()};
let ws = null, searchTimer = null;

const $ = id => document.getElementById(id);
async function api(path, opts){ const r = await fetch(API+path, opts); if(!r.ok) throw new Error(await r.text()); return r.json(); }
function fmtUtil(u){ if(!u||(!u.cpu_p95&&!u.mem_p95&&!u.disk_used_pct)) return '-'; return `${u.cpu_p95??'-'}/${u.mem_p95??'-'}/${u.disk_used_pct??'-'}`; }
function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
/* attr() — safe to inline a value into a double-quoted HTML attribute that
   wraps a single-quoted JS string, e.g. onclick="fn('${attr(x)}')".
   esc() alone is NOT enough there: HTML-decoding &#39; -> ' happens before JS
   runs, so a ' in x breaks out of the JS string. attr JS-escapes \ and ' first,
   then HTML-escapes & < > " for the attribute. Use esc() for text content,
   attr() for onclick/attribute+JS-string positions. */
function attr(s){ s=String(s??''); return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

/* shared support-bucket badge (data-gap) — warranty + OS-EOL both use the
   active/expiring/expired/unknown bucket the backend precomputes on every
   server row (s.warranty_bucket / s.os_eol_bucket). Used by the inventory list
   and the server drawer. */
const _BUCKET_COLOR = {active:'var(--green)',expiring:'var(--amber)',expired:'var(--red)',unknown:'#888'};
const _BUCKET_SHORT = {active:'act',expiring:'exp',expired:'EXP',unknown:'?'};
function bucketBadge(bucket, label){
  bucket = bucket || 'unknown';
  const c=_BUCKET_COLOR[bucket]||'#888';
  return `<span class="tag" style="color:${c}" title="${esc(label)}: ${esc(bucket)}">${esc(_BUCKET_SHORT[bucket]||bucket)}</span>`;
}

/* ---------- toast notifications ---------- */
function toast(msg, type='info', ms=3000){
  let wrap = document.querySelector('.toast-wrap');
  if(!wrap){ wrap = document.createElement('div'); wrap.className='toast-wrap'; document.body.appendChild(wrap); }
  const el = document.createElement('div'); el.className=`toast ${type}`; el.textContent=msg;
  wrap.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; el.style.transform='translateY(-8px)'; setTimeout(()=>el.remove(), 200); }, ms);
}

/* ---------- tabs ---------- */
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tabsec').forEach(x=>x.classList.add('hidden'));
  t.classList.add('active'); $('tab-'+t.dataset.tab).classList.remove('hidden');
  if(t.dataset.tab==='dashboard') loadDashboard();
  if(t.dataset.tab==='inventory') fetchInv();
  if(t.dataset.tab==='waves'){ loadWaves(); loadLzReadiness(); }
  if(t.dataset.tab==='code') loadCode();
  if(t.dataset.tab==='business-case') loadLatestBusinessCase();
  if(t.dataset.tab==='execution') loadExecution();
  if(t.dataset.tab==='readiness') loadReadiness();
  if(t.dataset.tab==='data-quality'){ loadDataGaps(); loadDiscovery(); }
});

/* ---------- header stats ---------- */
async function loadStats(){
  try{ const s = await api('/stats');
    $('stats').innerHTML = `<div class="stat">servers <b>${s.servers}</b></div><div class="stat">matches <b>${s.matches}</b></div><div class="stat">waves <b>${s.waves}</b></div>`;
  }catch(e){ $('stats').innerHTML='<div class="stat">backend <b>down</b></div>'; }
}

/* ---------- pipeline ops ---------- */
async function doIngest(){ await api('/ingest',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({source:'all'})}); await loadStats(); if(!$('tab-dashboard').classList.contains('hidden'))loadDashboard(); if(!$('tab-inventory').classList.contains('hidden'))fetchInv(); loadRail(); }
let _rebuildAbort = null;
const _REBUILD_PHASE = {
  normalize: 'normalize', 'persist:servers': 'write servers',
  'persist:workloads': 'write workloads', match: 'match targets',
  'persist:matches': 'write matches', plan: 'plan waves', 'persist:waves': 'write waves',
};
async function doRebuild(){
  const btn = $('rebuildBtn'), st = $('rebuildStatus');
  // a running rebuild -> this click cancels it (server-side work still finishes;
  // we just stop watching the stream and re-enable the button)
  if(_rebuildAbort){ _rebuildAbort.abort(); return; }
  _rebuildAbort = new AbortController();
  const sig = _rebuildAbort.signal;
  const mw = parseInt($('rebuildMaxWaves').value, 10);
  const body = {};
  if(mw && mw > 0) body.max_waves = mw;
  btn.disabled = true;
  st.innerHTML = '<span class="spinner"></span> rebuilding… <button class="sm" onclick="doRebuild()">cancel</button>';
  let resp;
  try{
    resp = await fetch(API+'/rebuild', {
      method:'POST', headers:{'content-type':'application/json'},
      body:JSON.stringify(body), signal:_rebuildAbort.signal
    });
  }catch(e){ _rebuildAbort=null; btn.disabled=false;
    st.textContent=''; if(e.name!=='AbortError') toast('Rebuild failed: '+e, 'err'); return; }
  if(!resp.ok){ _rebuildAbort=null; btn.disabled=false;
    st.innerHTML='<span class="ev-err">error</span>'; toast('Rebuild failed: '+await resp.text(), 'err'); return; }
  // NDJSON stream: one {type:progress|done|error} frame per line, like /strategy/batch.
  const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf='';
  let lastErr = null, t0 = Date.now();
  try{
    while(true){
      const {value, done:rd} = await reader.read();
      if(rd) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for(const line of lines){
        if(!line) continue;
        let j; try{ j = JSON.parse(line); }catch(e){ continue; }
        if(j.type === 'progress'){
          const label = _REBUILD_PHASE[j.phase] || j.phase;
          const extra = j.total!=null ? ` (${j.total})` : (j.servers!=null ? ` (${j.servers} servers)` : '');
          st.innerHTML = `<span class="spinner"></span> ${esc(label)}${extra} <button class="sm" onclick="doRebuild()">cancel</button>`;
        } else if(j.type === 'done'){
          const secs = ((Date.now()-t0)/1000).toFixed(1);
          st.innerHTML = `<span style="color:var(--green)">✓ rebuilt in ${secs}s</span>`;
          await loadStats();
          if(!$('tab-dashboard').classList.contains('hidden'))loadDashboard();
          if(!$('tab-inventory').classList.contains('hidden'))fetchInv();
          if(!$('tab-waves').classList.contains('hidden'))loadWaves();
          loadRail();
        } else if(j.type === 'error'){
          lastErr = j.error; st.innerHTML = '<span class="ev-err">error</span>';
        }
      }
    }
  }catch(e){ /* abort / network drop — handled below */ }
  const aborted = sig.aborted;
  _rebuildAbort = null; btn.disabled = false;
  if(lastErr) toast('Rebuild failed: '+lastErr, 'err');
  else if(aborted) st.textContent='';   // user cancelled the watch; rebuild still finishes server-side
  else if(st.textContent.startsWith('✓')) setTimeout(()=>{ if(st.textContent.startsWith('✓')) st.textContent=''; }, 4000);
  else st.innerHTML='<span class="ev-err">connection lost — rebuild may still be running</span>';
}
