/* idc-migrate core — shared globals/helpers, tab router, header stats, pipeline ops.
   Per-page logic lives in pages/*.js; main.js runs init. Loaded first. */
const API = location.origin + '/api';
let state = {q:'', filters:{}, page:1, page_size:50, order_by:'hostname', order_dir:'asc', last:null, selected:new Set()};
let ws = null, searchTimer = null;

const $ = id => document.getElementById(id);
async function api(path, opts){ const r = await fetch(API+path, opts); if(!r.ok) throw new Error(await r.text()); return r.json(); }
function fmtUtil(u){ if(!u||(!u.cpu_p95&&!u.mem_p95&&!u.disk_used_pct)) return '-'; return `${u.cpu_p95??'-'}/${u.mem_p95??'-'}/${u.disk_used_pct??'-'}`; }
function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

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
async function doRebuild(){
  const mw = parseInt($('rebuildMaxWaves').value, 10);
  const body = {};
  if(mw && mw > 0) body.max_waves = mw;
  await api('/rebuild',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  await loadStats();
  if(!$('tab-dashboard').classList.contains('hidden'))loadDashboard();
  if(!$('tab-inventory').classList.contains('hidden'))fetchInv();
  if(!$('tab-waves').classList.contains('hidden'))loadWaves();
  loadRail();
}
