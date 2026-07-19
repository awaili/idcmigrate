/* ---------- F8 — Landing Zone tab (archetype design + placement gate) ----------
   Surfaces what /lz/archetypes + /lz/readiness + /waves/{id}/lz-gate already
   return but the Waves-tab status card throws away: the full per-archetype
   blueprint (VPC / peering / egress / SGs / CAM / tag-policy / policy-as-code),
   the per-server classifier audit, the per-wave placement gate, and LZ IaC
   emit. Shares _LZ_ARCH_COLOR / _LZ_STATUS_BADGE / setLzStatus with waves.js. */
let _LZ = {archetypes:{}, counts:{}, per_server:[], readiness:{}};
let _lzClsFilter = 'all';
let _lzAppFilter = 'all';     // app_id select on the Server placement table
let _lzClsSearch = '';        // free-text (hostname / app_id) on the same table

// LZ scale tiers (small/medium/large) + the cross-scale golden rules, fetched
// from /api/lz/scale-tiers. The tier sets the DEFAULT STRATEGY (the comparison
// matrix); the operator's prompt customizes the rest. _lzScale is the tier the
// picker currently holds (defaults to the estate-inferred one from /api/lz/scale).
let _LZ_TIERS = [];           // [{scale,label,philosophy,...}, ...] in tier order
let _LZ_GOLDEN = [];          // [{key,rule,validator}, ...]
let _lzScale = '';            // the selected tier ('' until loaded)
let _LZ_INFERRED = null;      // the /api/lz/scale response (tier + signals + bands)

// Out-of-box LZ design scenarios — each chip appends a starter prompt into
// #lzDesignDemand so an operator can click, tweak, and hit "Design with
// MigraQ". Each scenario is tagged with the scale tier(s) it fits (``scales``);
// ``renderLzScenarios`` shows only the chips for the selected tier so picking
// "Small" never offers a 5-account hub-and-spoke that contradicts its flat
// default. Prompts build ON TOP of the scale tier's DEFAULT strategy (topology
// + governance) and only specify the USER-MODIFIABLE parts — which apps land in
// which account (app_map), extra compliance tiers, CIDR overrides — instead of
// re-declaring the peering/egress/governance the scale already fills. Each
// archetype IS a workload account (one VPC + one account); the engine routes a
// server to an account by which app it hosts (see lz.archetype_for).
// App ids below are the real estate's biggest apps per workload group:
//   Oracle/DB  : APP_0001, APP_0020, APP_0530, APP_0156, APP_0069, APP_0087, APP_0508
//   web/app    : APP_0073, APP_0468, APP_0030
//   infra/mgmt : APP_0278, APP_0260
//   big-data   : APP_0455 (hadoop/spark), APP_0006 (spark/kafka)
//   storage    : APP_0234 (minio), APP_0581 (gluster)
//   middleware : APP_0374 (zookeeper), APP_0335 (weblogic), APP_0610 (elasticsearch),
//                APP_0147 (rabbitmq), APP_0573 (websphere)
const _LZ_SCENARIOS = [
  // --- small (flat corp + online, NO dmz, NO hub transit) ---
  {label:'Flat — assign apps', scales:['small'],
   prompt:'Small estate — KEEP the flat 2-VPC default (corp + online, NO dmz tier, NO hub transit). Only assign apps: public web apps APP_0073, APP_0468, APP_0030 -> "online"; DB/internal apps APP_0001, APP_0020 -> "corp". role_map: web->online, every other role -> corp (no default catch-all — undetermined hosts stay pending for review). Do NOT add a dmz tier or hub-and-spoke — the small default is flat.'},
  {label:'Single account', scales:['small'],
   prompt:'Smallest footprint — collapse to ONE workload account: a single VPC "corp" (172.16.0.0/16) with DC peering + NAT, a public CLB for the web apps in the same VPC. app_map APP_0001, APP_0073 -> "corp"; role_map: every role -> corp (no default catch-all). Drop the separate online VPC.'},
  {label:'Promote app to online', scales:['small'],
   prompt:'Iterate: expose APP_0468 (currently in "corp") to the internet by moving it to "online". app_map APP_0468 -> "online". Keep the flat 2-VPC layout and the other assignments unchanged.'},

  // --- medium / large (hub-and-spoke; the scale fills topology + governance) ---
  {label:'Data + Online + Hub', scales:['medium','large'],
   prompt:'On top of the hub-and-spoke default (corp hub + online + dmz), assign apps to workload accounts by shape:\n- "data": no internet egress, DC via the hub -> DB apps APP_0001, APP_0020, APP_0530, APP_0156, APP_0069, APP_0087, APP_0508 (-> TencentDB/TData).\n- "online": public CLB + CDN + WAF -> web/app apps APP_0073, APP_0468, APP_0030.\n- "corp" hub: infra/mgmt APP_0278, APP_0260.\nrole_map db->data, web->online, app/infra/middleware/cache/hadoop/paas->corp (no default catch-all — undetermined hosts stay pending for review). Spokes reach the DC via corp only (no direct spoke->DC).'},
  {label:'+ Big-data account', scales:['medium','large'],
   prompt:'Add a big-data account to the hub-and-spoke: "bigdata" (no internet egress, Tencent EMR + CFS), peers only to corp for data ingest + the on-prem data lake. app_map APP_0455 (hadoop/spark), APP_0006 (spark/kafka) -> "bigdata". Keep data (APP_0001, APP_0020, APP_0530), online (APP_0073, APP_0468), corp hub as before.'},
  {label:'+ Storage account', scales:['medium','large'],
   prompt:'Add a storage account: "storage" (no internet egress, self-hosted object/file -> COS + CFS), peers only to corp. app_map APP_0234 (minio), APP_0581 (gluster) -> "storage". Keep data/online/corp as before.'},
  {label:'+ Middleware account', scales:['medium','large'],
   prompt:'Split the middleware runtimes into their own account: "middleware" (no internet egress -> TKE/TDMQ/TSE), peers only to corp. app_map APP_0374 (zk), APP_0335 (weblogic), APP_0610 (es), APP_0147 (rabbitmq), APP_0573 (websphere) -> "middleware". Keep data/online/corp as before.'},
  {label:'Regulated / PCI', scales:['medium','large'],
   prompt:'Add a PCI-isolated account to the hub-and-spoke: "pci" (NO internet egress, no public CLB, CAM scoped to PCI operators, mandatory encryption, tag_policy env=pci), peers only to corp — NEVER to online/data directly. app_map APP_0156, APP_0069 -> "pci". Keep online (APP_0073, APP_0468), data (APP_0001, APP_0020, APP_0530), corp hub.'},

  // --- large-only (build on the CFW + SCP + centralized-logging defaults) ---
  {label:'Enterprise + CFW/SCP', scales:['large'],
   prompt:'Large enterprise — KEEP the large-tier defaults (hub-and-spoke + Cloud Firewall inspection on all inter-VPC traffic, SCP guardrails, centralized ActionTrail in a logging account). Split workloads across accounts by shape: data (APP_0001, APP_0020, APP_0530), online (APP_0073, APP_0468, APP_0030), bigdata (APP_0455, APP_0006), storage (APP_0234, APP_0581), middleware (APP_0374, APP_0335, APP_0610), corp hub. Every spoke peers only to corp; CFW inspects east-west traffic.'},
  {label:'Air-gapped enterprise', scales:['large'],
   prompt:'Air-gapped regulated enterprise — NO internet egress anywhere. Keep the large-tier governance (SCP guardrails, centralized ActionTrail, encryption) but drop NAT/public CLB/CDN/WAF. Accounts: data (APP_0001, APP_0020, APP_0530, APP_0156), app (APP_0073, APP_0468, APP_0030), corp hub (DC peering only). All peer only via corp.'},

  // --- iterate (tweaks to the customizable parts; hub/multi-account designs) ---
  {label:'Move app between accounts', scales:['medium','large'],
   prompt:'Iterate: move APP_0087 (a postgres app currently in "data") into "online" so its app tier sits with the public apps. app_map APP_0087 -> "online" (overrides role_map db->data for this one app). Keep APP_0001, APP_0020, APP_0530 -> "data" and the corp hub unchanged.'},
  {label:'Rename accounts', scales:['medium','large'],
   prompt:'Iterate: rename the workload accounts (each archetype IS one account, so this renames the archetypes): "data" -> "data-svc", "online" -> "public-apps", "corp" -> "hub-mgmt". Keep the app_map, peering, and VPC CIDRs unchanged — only the account/archetype names change.'},
  {label:'Collapse data + online', scales:['medium','large'],
   prompt:'Iterate: collapse the "data" and "online" archetypes into ONE workload archetype "workloads" (one account, one VPC) to cut the account count. app_map APP_0001, APP_0020, APP_0530 (was data) AND APP_0073, APP_0468, APP_0030 (was online) -> "workloads"; keep "corp" as the hub. Drop the separate data/online VPCs and re-check CIDR non-overlap.'},
  {label:'Shrink hub CIDR', scales:['medium','large'],
   prompt:'Iterate: change the hub ("corp") VPC CIDR to 10.8.0.0/16 and re-check that no account VPC overlaps it or the on-prem ranges. Do not change any app_map assignment.'},
];
function _lzScenariosFor(scale){
  const cur = scale || 'medium';
  return _LZ_SCENARIOS.map((s,i)=>({s,i})).filter(({s})=>
    !s.scales || s.scales.length === 0 || s.scales.includes('all')
    || s.scales.includes(cur));
}
function renderLzScenarios(){
  const el = $('lzDesignScenarios'); if(!el) return;
  const shown = _lzScenariosFor(_lzScale);
  el.innerHTML = shown.length
    ? shown.map(({s,i})=>
        `<button class="sm" onclick="fillLzScenario(${i})" title="${attr(s.prompt)}">${esc(s.label)}</button>`).join('')
    : '<span class="muted">no starter prompts for this scale — describe your own below.</span>';
}
// Shared append-to-prompt-box helper used by BOTH the LZ design scenarios and
// the Network designer templates: APPEND with a divider (an operator stacks
// several requirements, not just one), skip exact duplicates, scroll to the
// new text. `submitVerb` is just for the toast wording.
function _appendPromptTo(taId, prompt, label, submitVerb){
  const ta = $(taId); if(!ta) return;
  const cur = ta.value.trim();
  if(cur && cur.includes(prompt)){
    toast('"'+label+'" is already in the prompt', 'warn', 2200);
    return;
  }
  ta.value = cur ? cur + '\n\n---\n' + prompt : prompt;
  ta.focus(); ta.scrollTop = ta.scrollHeight;
  toast(cur ? ('Appended "'+label+'" — add more, then '+submitVerb)
            : ('Loaded "'+label+'"'), 'info', 2600);
}
function fillLzScenario(i){
  const s = _LZ_SCENARIOS[i]; if(!s) return;
  _appendPromptTo('lzDesignDemand', s.prompt, s.label, 'Design with MigraQ');
}

// Out-of-box NETWORK DESIGNER carving templates — each chip appends a starter
// prompt into #ndDemand (the carving-policy description MigraQ turns into
// {subnet_prefix, tier_order, tiers, default_tier}). Stack several; leave blank
// to use the deterministic default (4 tier subnets per VPC, auto-sized).
// NOTE: account add/rename/merge and VPC CIDRs are Layer 1 (the LZ design above)
// — DO NOT add account-topology scenarios here. The network policy only carves
// subnets and places hosts inside the VPCs the active LZ design already fixed.
const _ND_SCENARIOS = [
  {label:'4-tier auto (default)', prompt:'Carve each account VPC into the four tier subnets (web, app, data, infra), auto-sized. Group web/frontend->web, app/general->app, db/cache/hadoop->data, k8s/monitoring/infra->infra.'},
  {label:'/24 subnets', prompt:'Use /24 subnets for every tier (subnet_prefix 24) across all account VPCs.'},
  {label:'/23 (more headroom)', prompt:'Use /23 subnets for every tier (subnet_prefix 23) — more headroom for estates with large DB/data hosts.'},
  {label:'db+cache+hadoop -> data', prompt:'Group db, cache, and hadoop roles into the data tier; web/frontend into web; app/general/api into app; k8s/monitoring/infra into infra. Keep all four tiers.'},
  {label:'Flat single tier', prompt:'Simple lift-and-shift: collapse to a single tier — put every role in the app tier (tier_order ["app"]), one subnet per VPC.'},
  {label:'Web + data only', prompt:'Only carve the web and data tiers (tier_order ["web","data"]); drop the app and infra tiers.'},
  {label:'3 tiers public/private/protected', prompt:'Carve 3 tier subnets instead of the default 4: tier_order ["public","private","protected"], default_tier "private". Group web/frontend -> public; app/general/api -> private; db/cache/hadoop/k8s/monitoring/infra -> protected.'},
  {label:'Web + data only (2 tiers)', prompt:'Carve only 2 tier subnets: tier_order ["web","data"], default_tier "web". Group web/frontend/app/general/api -> web; db/cache/hadoop/k8s/monitoring/infra -> data. Drop the app and infra tiers.'},
];
function renderNdScenarios(){
  const el = $('ndScenarios'); if(!el) return;
  el.innerHTML = _ND_SCENARIOS.map((s,i)=>
    `<button class="sm" onclick="fillNdScenario(${i})" title="${attr(s.prompt)}">${esc(s.label)}</button>`).join('');
}
function fillNdScenario(i){
  const s = _ND_SCENARIOS[i]; if(!s) return;
  _appendPromptTo('ndDemand', s.prompt, s.label, 'Generate');
}

async function loadLz(){
  renderLzScenarios();
  renderNdScenarios();
  await loadLzScaleTiers();      // scale picker + matrix + golden rules
  await loadLzArchetypes();
  await loadNetworkDesigner();   // sets _ND so classification can reuse it
  loadLzClassification();
  loadLzDesigns();
  // NOTE: the per-wave Placement gate lives on the WAVES tab (a wave must
  // exist before its gate is meaningful) — see loadLzGate / app.js waves init.
}

/* Scale tier (small/medium/large) — the DEFAULT STRATEGY axis. The tier drives
   the comparison matrix (account/networking/security/audit/budgeting); the
   operator's prompt customizes the user-modifiable parts (app placement, CIDRs,
   names, compliance). The 3 golden rules apply at every scale. */
async function loadLzScaleTiers(){
  try{
    const [tiers, inferred] = await Promise.all([api('/lz/scale-tiers'), api('/lz/scale')]);
    _LZ_TIERS = tiers.tiers || [];
    _LZ_GOLDEN = tiers.golden_rules || [];
    _lzScale = inferred.scale || (tiers.order && tiers.order[1]) || 'medium';
    renderLzScalePicker(inferred);
  }catch(e){
    const el = $('lzScaleMatrix'); if(el) el.innerHTML = '<span class="ev-err">'+esc(String(e))+'</span>';
  }
}

function _fmt(n){ return (n||0).toLocaleString(); }

function renderLzScalePicker(inferred){
  const sel = $('lzScale'); if(!sel) return;
  _LZ_INFERRED = inferred || null;
  sel.innerHTML = _LZ_TIERS.map(t=>
    `<option value="${attr(t.scale)}" ${t.scale===_lzScale?'selected':''}>${esc(t.label)}</option>`).join('');
  const sig = $('lzScaleSig');
  if(sig && inferred && inferred.signals){
    const s = inferred.signals;
    sig.innerHTML = `inferred: <b>${esc(inferred.label)}</b> — ${_fmt(s.servers)} servers · ${_fmt(s.cores)} cores · ${_fmt(s.memory_gb)} GB${s.baremetal?` · ${_fmt(s.baremetal)} bare-metal`:''} · ${_fmt(s.apps)} apps${s.regulated?' · <b style="color:var(--red)">regulated</b>':''}`;
  }
  renderLzScaleMatrix();
  renderLzGoldenRules();
  renderLzScenarios();   // chips match the inferred scale
}

function onLzScaleChange(){
  const sel = $('lzScale'); if(!sel) return;
  _lzScale = sel.value;
  renderLzScaleMatrix();
  renderLzGoldenRules();
  renderLzScenarios();   // chips follow the operator's chosen tier
}

// the comparison matrix — rows are the matrix dimensions, columns are the 3
// tiers. The selected tier's column is highlighted so the operator sees which
// defaults their design will inherit. The first rows are the concrete sizing
// bands (servers / cores / memory / apps / accounts) with the live estate's
// actual values shown under the inferred tier so the operator can compare.
function renderLzScaleMatrix(){
  const el = $('lzScaleMatrix'); if(!el || !_LZ_TIERS.length) return;
  const inferred = _LZ_INFERRED, sig = inferred && inferred.signals, bands = inferred && inferred.bands;
  const live = (label, key, fmtVal) => {
    if(!sig || bands===undefined) return '';
    const mine = fmtVal(sig[key]);
    return `<div style="font-size:10px;color:var(--green);margin-top:2px">your estate: <b>${mine}</b></div>`;
  };
  const dims = [
    ['philosophy', 'Philosophy', null],
    ['target', 'Target', null],
    ['_size', 'Estate size', null],   // special: rendered from estate_size sub-keys
    ['account_strategy', 'Account strategy', null],
    ['networking', 'Networking', null],
    ['security', 'Security', null],
    ['audit', 'Audit', null],
    ['budgeting', 'Budgeting', null],
    ['identity_rule', 'Identity', null],
  ];
  const head = '<tr><th>dimension</th>' + _LZ_TIERS.map(t=>
    `<th${t.scale===_lzScale?' style="color:var(--green)"':''}>${esc(t.label)}${t.scale===_lzScale?' ●':''}</th>`).join('') + '</tr>';
  const rows = dims.map(([k,label,_])=>{
    if(k === '_size'){
      const sub = [['servers','Servers', v=>_fmt(v)],
                   ['cores','vCPU cores', v=>_fmt(v)],
                   ['memory_gb','Memory (GB)', v=>_fmt(v)],
                   ['apps','Apps', v=>_fmt(v)],
                   ['accounts','Accounts', v=>esc(v)]];
      return sub.map(([sk,slabel,fn])=>{
        const liveLine = (sk==='accounts') ? '' : live(slabel, sk, fn);
        return '<tr><td><b>'+esc(slabel)+'</b></td>' + _LZ_TIERS.map(t=>{
          const v = (t.estate_size||{})[sk] || '—';
          const hi = t.scale===_lzScale ? ' style="background:var(--bg2)"' : '';
          const lv = (t.scale===_lzScale && liveLine) ? '<br>'+liveLine : '';
          return `<td${hi}>${esc(v)}${lv}</td>`;
        }).join('') + '</tr>';
      }).join('');
    }
    return '<tr><td><b>'+esc(label)+'</b></td>' + _LZ_TIERS.map(t=>{
      const v = t[k]||'';
      const hi = t.scale===_lzScale ? ' style="background:var(--bg2)"' : '';
      return `<td${hi}>${esc(v)}</td>`;
    }).join('') + '</tr>';
  }).join('');
  el.innerHTML = `<table class="tbl" style="font-size:11px"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function renderLzGoldenRules(){
  const el = $('lzGoldenRules'); if(!el) return;
  el.innerHTML = _LZ_GOLDEN.length
    ? `<div class="muted" style="font-size:11px;margin-bottom:2px">golden rules — every scale (enforced as governance warnings):</div>` +
      _LZ_GOLDEN.map(r=>`<div style="font-size:11px;margin-top:2px"><b>${esc(r.key)}:</b> ${esc(r.rule)}</div>`).join('')
    : '';
}

/* Section A — archetype cards: lifecycle + expandable blueprint + IaC. */
async function loadLzArchetypes(){
  const out = $('lzArchCards'); if(!out) return;
  out.innerHTML = '<span class="spinner"></span>';
  try{
    const arch = await api('/lz/archetypes');
    const rdy  = await api('/lz/readiness');
    _LZ.archetypes = arch.archetypes || {};
    _LZ.counts = arch.counts || {};
    _LZ.per_server = arch.per_server || [];
    _LZ.readiness = rdy || {};
    const archNames = Object.keys(_LZ.archetypes);   // active design's archetypes (built-in or custom)
    out.innerHTML = archNames.length ? archNames.map(_lzArchCard).join('') : '<span class="muted">no archetypes in the active design</span>';
  }catch(e){ out.innerHTML = '<span class="ev-err">'+esc(String(e))+'</span>'; }
}

function _lzArchCard(a){
  const r = (_LZ.readiness[a]||{});
  const bp = _LZ.archetypes[a]||{};
  const sb = _LZ_STATUS_BADGE[r.status||'not_ready'];
  const ready = r.ready, gv = r.guardrail_pass;
  const setSt = (s)=>`setLzStatus('${a}','${s}')`;
  const peer = bp.peering||{}, eg = bp.internet_egress||{}, tp = bp.tag_policy||{};
  const pol = bp.policy_as_code||[], cam = bp.cam_roles||[], sg = bp.security_groups||[];
  const egressBits = ['nat','public_clb','cdn','waf'].filter(k=>eg[k]!==undefined).map(k=>`${k}:${eg[k]}`).join(' · ');
  // guardrail chip: ✓ pass / ✗ fail / "not evaluated" (finalized but no IaC
  // artifact on file — green "ready" without this would mislead the operator
  // into thinking guardrails were checked when none exist).
  const gvChip = gv===false
    ? '<span class="tag" style="color:var(--red)">guardrails ✗ ('+(r.guardrail_failing||0)+')</span>'
    : gv===true ? '<span class="tag" style="color:var(--green)">guardrails ✓</span>'
    : '<span class="tag" style="color:var(--amber)">guardrails not evaluated</span>';
  return `<div class="mcard" style="padding:12px;margin-top:8px">
    <div class="row" style="justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
      <div>${_lzArchBadge(a)} <b>${esc(bp.summary||a)}</b>
        <span class="tag" style="color:${sb[1]}">${sb[0]}</span>
        ${ready?'<span class="tag" style="color:var(--green)">ready</span>':'<span class="tag" style="color:var(--amber)">gated</span>'}
        <span class="muted">${r.workload_count||0} workload server(s)</span>
        ${gvChip}
      </div>
      <div class="row" style="gap:6px">
        <button class="sm" onclick="${setSt('applied')}">mark applied</button>
        <button class="sm primary" onclick="${setSt('finalized')}">mark finalized</button>
        <button class="sm" onclick="${setSt('not_ready')}">reset</button>
        <button class="sm" onclick="lzToggleBp('${a}')">blueprint</button>
        <button class="sm" onclick="lzEmitIac('${a}')">emit IaC</button>
      </div>
    </div>
    <div id="lzBp_${a}" class="hidden" style="margin-top:8px;font-size:12px">
      <div class="grid2col">
        <div><span class="muted">VPC</span><br><b>${esc(bp.vpc&&bp.vpc.name||'-')}</b> <span class="muted">${esc(bp.vpc&&bp.vpc.cidr||'')}</span><br><span class="muted">${esc(bp.vpc&&bp.vpc.description||'')}</span></div>
        <div><span class="muted">peering</span><br>dc:${peer.to_dc} · corp:${(peer.to_corp===null||peer.to_corp===undefined)?'-':peer.to_corp} · online:${(peer.to_online===null||peer.to_online===undefined)?'-':peer.to_online} · dmz:${(peer.to_dmz===null||peer.to_dmz===undefined)?'-':peer.to_dmz}<br><span class="muted">${esc(peer.description||'')}</span></div>
        <div><span class="muted">internet egress</span><br>${esc(egressBits||'-')}</div>
        <div><span class="muted">tag policy</span><br>${Object.keys(tp).map(k=>`${esc(k)}=${esc(tp[k])}`).join(', ')||'-'}</div>
        <div><span class="muted">security groups</span><br>${sg.map(esc).join(', ')||'-'}</div>
        <div><span class="muted">CAM roles</span><br>${cam.map(esc).join(', ')||'-'}</div>
      </div>
      <div style="margin-top:6px"><span class="muted">policy-as-code</span><ul style="margin:2px 0 0 18px">${pol.map(p=>`<li>${esc(p)}</li>`).join('')||'<li class="muted">—</li>'}</ul></div>
      <div id="lzIac_${a}" style="margin-top:6px"></div>
    </div>
  </div>`;
}

function lzToggleBp(a){
  const el = $('lzBp_'+a); el.classList.toggle('hidden');
  if(!el.classList.contains('hidden')) lzShowIac(a);  // lazy-load any existing artifact
}

/* Section B — Server placement (which servers land where). ONE merged table that
   shows each server's classifier archetype (the account it lands in) AND — when a
   network design is active — its live placement (account / VPC / subnet / tier /
   IP). Replaces the old "Estate classification" card + the in-Network-designer
   "Server placement" table, which showed the same per-server placement twice (one
   keyed off _ND.placements with an inline subnet-move dropdown, one keyed off
   _LZ.per_server with a click-to-open drawer). The drawer is the single move UI
   now; the per_server placement already carries tier, so the merged table adds it.
   The unified filter bar (archetype chips + app dropdown + free-text search)
   narrows the table; click a row to open the placement drawer. */
async function loadLzClassification(){
  const out = $('lzCls'); if(!out) return;
  // per_server rows carry their placement (account/vpc/subnet/tier/ip) from the
  // active network design, attached server-side (so fqdn-only servers match —
  // identity_key is fqdn-or-hostname, not just hostname), plus app_ids so the
  // table can show and filter by application.
  const total = _LZ.per_server.length;
  const hasNd = _LZ.per_server.some(s => s.placement);
  // app rollup over the current estate (count of servers per app) — the dropdown
  // IS the by-app classification stat; picking an app filters the table to it.
  const appCounts = {};
  for(const s of _LZ.per_server) for(const a of (s.app_ids||[])) appCounts[a]=(appCounts[a]||0)+1;
  const apps = Object.entries(appCounts).sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0]));
  const q = _lzClsSearch.toLowerCase().trim();
  const rows = _LZ.per_server.filter(s=>{
    if(_lzClsFilter!=='all' && s.archetype!==_lzClsFilter) return false;
    if(_lzAppFilter!=='all' && !((s.app_ids||[]).includes(_lzAppFilter))) return false;
    if(q){
      const p = s.placement||{};
      const hay = ((s.hostname||'')+' '+((s.app_ids||[]).join(' '))+' '+(s.role||'')
                   +' '+(p.subnet||'')+' '+(p.vpc||'')+' '+(p.account||'')+' '+(p.tier||'')).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });
  const body = rows.slice(0,1000).map(s=>{
    const p = s.placement || {};
    const ip = p.managed
      ? '<span class="tag" style="color:var(--amber)" title="managed/PaaS target — no per-host VPC subnet IP">PaaS</span>'
      : (p.ip ? esc(p.ip) : (hasNd ? '<span class="muted">—</span>' : ''));
    const key = attr(s.identity_key || s.hostname || s.server_id);
    const vpcPin = p.arch_overridden ? '<span class="tag" style="color:var(--amber)" title="VPC pin">vpc-pin</span> ' : '';
    const tierCell = p.tier
      ? `<span style="color:${_TIER_COLOR[p.tier]||'#888'}">${esc(p.tier)}</span>`
      : (hasNd ? '<span class="muted">—</span>' : '');
    const appTags = (s.app_ids||[]);
    const appCell = appTags.length ? appTags.map(a=>`<span class="tag" style="cursor:pointer" onclick="event.stopPropagation();_lzAppFilter='${attr(a)}';loadLzClassification()" title="filter to ${attr(a)}">${esc(a)}</span>`).join(' ') : '<span class="muted">—</span>';
    return `<tr style="cursor:pointer" onclick="openPlacementDrawer('${key}')">
      <td data-label="hostname"><b>${esc(s.hostname||s.server_id)}</b></td>
      <td data-label="app">${appCell}</td>
      <td data-label="role"><span class="tag ${esc(s.role||'')}">${esc(s.role||'-')}</span></td>
      <td data-label="archetype">${_lzArchBadge(s.archetype)}</td>
      <td data-label="tier">${tierCell}</td>
      <td data-label="account">${p.account_overridden?'<span class="tag" style="color:var(--amber)" title="account pin">pin</span> ':''}${esc(p.account||'-')}</td>
      <td data-label="vpc">${vpcPin}${esc(p.vpc||'-')}</td>
      <td data-label="subnet">${p.overridden?'<span class="tag" style="color:var(--amber)" title="operator pin">pin</span> ':''}${esc(p.subnet||'-')}</td>
      <td data-label="ip">${ip}</td>
    </tr>`;
  }).join('');
  const chips = Object.keys(_LZ.archetypes).map(a=>
    `<button class="sm ${_lzClsFilter===a?'primary':''}" onclick="_lzClsFilter='${a}';loadLzClassification()">${_lzArchBadge(a)} ${_LZ.counts[a]||0}</button>`).join(' ');
  // undetermined hosts (no classifier rule matched) surface as a "pending" chip
  // so the operator can filter to them and classify via a tag / app_map / role_map
  // rule or a VPC pin — they are NOT auto-dumped into the corp/hub account.
  const pendingN = _LZ.counts['pending'] || 0;
  const pendingChip = pendingN
    ? `<button class="sm ${_lzClsFilter==='pending'?'primary':''}" onclick="_lzClsFilter='pending';loadLzClassification()" title="undetermined — no classifier rule matched; classify before placement">${_lzArchBadge('pending')} ${pendingN}</button>`
    : '';
  const appOpts = `<option value="all" ${_lzAppFilter==='all'?'selected':''}>all apps (${apps.length})</option>` +
    apps.map(([a,n])=>`<option value="${attr(a)}" ${_lzAppFilter===a?'selected':''}>${esc(a)} (${n})</option>`).join('');
  const note = hasNd
    ? `<span class="muted" style="font-size:12px">placements from the active network design — <b>click a row</b> to change a server's account / VPC / subnet.</span>`
    : `<span class="muted" style="font-size:12px">no active network design — VPC / subnet / tier / IP columns fill in once you generate one in the <b>Network designer</b> card above.</span>`;
  out.innerHTML = `<div class="row" style="gap:6px;margin-bottom:8px;flex-wrap:wrap;align-items:center">
      <button class="sm ${_lzClsFilter==='all'?'primary':''}" onclick="_lzClsFilter='all';loadLzClassification()">all ${total}</button>
      ${chips}
      ${pendingChip}
    </div>
    <div class="row" style="gap:8px;margin-bottom:8px;flex-wrap:wrap;align-items:center">
      <select id="lzAppSel" onchange="_lzAppFilter=this.value;loadLzClassification()" style="min-width:200px" title="filter the placement table by application">${appOpts}</select>
      <input id="lzClsSearch" placeholder="filter by hostname, app, role, subnet…" style="min-width:220px;flex:1" oninput="_lzClsSearch=this.value;loadLzClassification()" value="${attr(_lzClsSearch)}" title="free-text filter on hostname, app id, role, subnet, VPC, tier, or account"/>
      <button class="sm" onclick="_lzAppFilter='all';_lzClsSearch='';loadLzClassification()">reset</button>
      <span class="muted" style="font-size:12px">${rows.length} server${rows.length!==1?'s':''}${_lzAppFilter!=='all'?` in ${esc(_lzAppFilter)}`:''}</span>
    </div>
    ${note}
    <div class="xscroll" style="max-height:60vh;overflow:auto;margin-top:6px"><table class="tbl mcard"><thead><tr>
      <th>hostname</th><th>app</th><th>role</th><th>archetype</th><th>tier</th><th>account</th><th>VPC</th><th>subnet</th><th>IP</th>
    </tr></thead>
      <tbody>${body||'<tr><td colspan="9" class="muted">no servers</td></tr>'}</tbody></table></div>
    ${rows.length>1000?`<div class="muted" style="font-size:11px;margin-top:4px">showing first 1000 of ${rows.length}</div>`:''}`;
}

/* Section B2 — placement drawer: click a host in the Server placement table
   to see the VPC it's allocated to and change it. VPC is 1:1 with archetype, so
   the VPC dropdown lists the active design's archetypes; the subnet dropdown
   lists the chosen VPC's carved subnets. Pins survive regenerate (they ride the
   active network design). Reuses the global #drawer / #dTitle / #dBody that
   server-drawer.js uses. */
let _plDrawerHost = null;   // identity_key of the host shown in the drawer

function _ndVpcs(){ return (_ND && _ND.vpcs) ? _ND.vpcs : []; }

// cascade option builders for the placement drawer (account → VPC → subnet).
// _plVpcOptions filters VPCs to one account ('' = all); _plSubnetOptions lists
// one VPC's subnets; _plAcctOptions lists every account in the design.
function _plAcctOptions(sel){
  const accts = Array.from(new Set(_ndVpcs().map(v=>v.account).filter(Boolean))).sort();
  return accts.map(a=>`<option value="${attr(a)}" ${a===sel?'selected':''}>${esc(a)}</option>`).join('');
}
function _plVpcOptions(acct, selArch){
  const list = acct ? _ndVpcs().filter(v=>v.account===acct) : _ndVpcs();
  return list.map(v=>`<option value="${attr(v.archetype)}" ${v.archetype===selArch?'selected':''}>${esc(v.name||v.archetype)} (${esc(v.archetype)}, ${esc(v.account||'-')})</option>`).join('');
}
function _plSubnetOptions(arch, selSn){
  const vpc = _ndVpcs().find(v=>v.archetype===arch);
  return (vpc && vpc.subnets||[]).map(s=>`<option value="${attr(s.name)}" ${s.name===selSn?'selected':''}>${esc(s.name)} (${esc(s.tier||'-')}, used ${s.used||0}/${s.usable||s.capacity||0})</option>`).join('');
}

function openPlacementDrawer(host){
  _plDrawerHost = host;
  const nd = _ND;
  $('dTitle').innerHTML = 'Placement · <span class="muted" style="font-size:13px">'+esc(host)+'</span>';
  if(!nd || !nd.placements){
    $('dBody').innerHTML = '<p class="muted">no active network design — generate + apply one in the <b>Network designer</b> card below to place servers into VPCs.</p>';
    openDrawer(); return;
  }
  const p = nd.placements[host];
  if(!p){
    $('dBody').innerHTML = '<p class="muted">no placement for '+esc(host)+' — fqdn-only / unnamed servers are placed but can\'t be pinned by hostname.</p>';
    openDrawer(); return;
  }
  const vpcs = _ndVpcs();
  // cascade: account (top) → VPC → subnet. account is 1:1 with archetype, so
  // it defaults to the current VPC's account — that keeps the server's current
  // VPC visible in the VPC dropdown. VPC options are filtered to the chosen
  // account; subnet options to the chosen VPC.
  const curVpc = vpcs.find(v=>v.archetype===p.archetype) || {};
  const vpcAcct = curVpc.account || p.account || '';
  const acctOpts = _plAcctOptions(vpcAcct);
  const vpcOpts  = _plVpcOptions(vpcAcct, p.archetype);
  const snOpts  = _plSubnetOptions(p.archetype, p.subnet);
  const archPin = p.arch_overridden ? '<span class="tag" style="color:var(--amber)">VPC pin</span>' : '<span class="muted">classifier</span>';
  const snPin = p.overridden ? '<span class="tag" style="color:var(--amber)">subnet pin</span>' : '<span class="muted">tier default</span>';
  const acctPin = p.account_overridden ? '<span class="tag" style="color:var(--amber)">account pin</span>' : '<span class="muted">VPC default</span>';
  const kv = (label, val) => `<div class="kv-row"><span class="kv-label">${label}</span><span class="kv-val">${val}</span></div>`;
  $('dBody').innerHTML = `
    <div class="kv" style="margin-bottom:10px">
      ${kv('role', `<span class="tag ${esc(p.role||'')}">${esc(p.role||'-')}</span>`)}
      ${kv('archetype', `${_lzArchBadge(p.archetype)} <span class="muted" style="font-size:11px">(${archPin})</span>`)}
      ${kv('account', `<b>${esc(p.account||'-')}</b> <span class="muted" style="font-size:11px">(${acctPin})</span>`)}
      ${kv('VPC', `<b>${esc(p.vpc||'-')}</b>`)}
      ${kv('subnet', `<b>${esc(p.subnet||'-')}</b> <span class="muted" style="font-size:11px">(${snPin})</span>`)}
      ${kv('IP', p.ip?`<b>${esc(p.ip)}</b>`:'<span class="muted">—</span>')}
    </div>
    <div class="muted" style="font-size:11px;margin-bottom:8px">Change where this server lands. Pick an <b>account</b> first — the VPC list narrows to that account's VPCs; pick a <b>VPC</b> and the subnet list narrows to that VPC's subnets. Account / VPC / subnet changes all survive regenerate.</div>
    <label class="muted" style="font-size:12px">account</label>
    <select id="plAcct" style="width:100%;margin:2px 0 10px" onchange="_plOnAcctChange()">${acctOpts}
    </select>
    <label class="muted" style="font-size:12px">VPC (archetype)</label>
    <select id="plVpc" style="width:100%;margin:2px 0 10px" onchange="_plOnVpcChange()">
      <option value="">— revert to classifier —</option>${vpcOpts}
    </select>
    <label class="muted" style="font-size:12px">subnet</label>
    <select id="plSubnet" style="width:100%;margin:2px 0 10px">
      <option value="">— revert to tier —</option>${snOpts}
    </select>
    <div class="row" style="gap:8px;margin-top:6px">
      <button class="primary" onclick="applyPlacement()">Apply</button>
      <button onclick="closeDrawer()">Cancel</button>
    </div>
    <div id="plErr" style="margin-top:8px"></div>`;
  openDrawer();
}

// account → VPC → subnet cascade. Picking an account re-populates the VPC
// dropdown with only that account's VPCs: keep the current VPC when it still
// belongs to the chosen account, otherwise default to the account's first VPC
// (so an account change always lands the server in a VPC of that account).
function _plOnAcctChange(){
  const acctSel = $('plAcct'); if(!acctSel) return;
  const acct = acctSel.value;
  const vpcSel = $('plVpc'); if(!vpcSel) return;
  const vpcs = _ndVpcs();
  const curArch = vpcSel.value;
  let keep = '';
  if(curArch && vpcs.some(v=>v.archetype===curArch && (acct==='' || v.account===acct))){
    keep = curArch;
  } else if(acct){
    keep = (vpcs.find(v=>v.account===acct)||{}).archetype || '';
  }
  vpcSel.innerHTML = '<option value="">— revert to classifier —</option>'+_plVpcOptions(acct, keep);
  _plOnVpcChange();
}

// picking a VPC re-populates the subnet dropdown with that VPC's subnets (a
// stale subnet pin doesn't carry across VPCs).
function _plOnVpcChange(){
  const vpcSel = $('plVpc'); if(!vpcSel) return;
  const arch = vpcSel.value;
  const subnetSel = $('plSubnet');
  const curSn = subnetSel && subnetSel.value;
  const vpc = _ndVpcs().find(v=>v.archetype===arch);
  const keep = curSn && vpc && (vpc.subnets||[]).some(s=>s.name===curSn) ? curSn : '';
  if(subnetSel) subnetSel.innerHTML = '<option value="">— revert to tier —</option>'+_plSubnetOptions(arch, keep);
}

async function applyPlacement(){
  const host = _plDrawerHost; if(!host) return;
  const vpc = $('plVpc') && $('plVpc').value;
  const subnet = $('plSubnet') && $('plSubnet').value;
  // only send the fields the operator changed from the current placement, so a
  // no-op Apply doesn't needlessly drop a pin.
  const p = (_ND && _ND.placements && _ND.placements[host]) || {};
  const body = {hostname: host, by:'operator'};
  // VPC: send only if it differs from the current archetype.
  if(vpc && vpc !== (p.archetype||'')) body.archetype = vpc;
  // subnet: send only if it differs from the current subnet.
  if(subnet && subnet !== (p.subnet||'')) body.subnet = subnet;
  // account follows the VPC under the cascade (account is 1:1 with archetype),
  // so the backend already derives it from the VPC. We only send account to
  // normalize a pre-existing account pin to the target VPC's account — never
  // to create a new one.
  if(p.account_overridden){
    const selVpc = _ndVpcs().find(v=>v.archetype===vpc);
    const effAcct = (selVpc && selVpc.account) || '';
    if(effAcct !== (p.account||'')) body.account = effAcct;
  }
  const err = $('plErr'); if(err) err.innerHTML = '<span class="spinner"></span>';
  try{
    const r = await api('/lz/network/placement', {method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    // full refresh so the merged Server placement table + Network designer card +
    // drawer agree (the table reads _LZ.per_server, which loadLzArchetypes refetches
    // with the updated placement attached server-side).
    await loadLzArchetypes();      // refreshes _LZ.per_server (counts + arch overlay + placement)
    await loadNetworkDesigner();   // refreshes _ND (placements + validation + topo)
    loadLzClassification();
    toast(`moved ${host} → ${r.placement.vpc||'-'} / ${r.placement.subnet||'-'}`,'ok');
    openPlacementDrawer(host);     // re-open with the refreshed placement
  }catch(e){
    if(err) err.innerHTML = '<div class="ev-err">'+esc(String(e))+'</div>';
    else toast('move failed: '+e,'err');
  }
}

/* Section C — per-wave placement gate. Rendered on the WAVES tab (a wave must
   exist before its gate is meaningful), so loadLzGate is wired into the waves
   tab init in app.js + into applyPlan, NOT into loadLz(). It ALWAYS re-fetches
   /waves so the dropdown tracks the wave design shown above (it used to reuse a
   stale global WAVES, so after a re-plan the gate listed the OLD waves while the
   list above already showed the new ones). */
async function loadLzGate(){
  const sel = $('lzGateWave'); if(!sel) return;
  try{ WAVES = await api('/waves'); }catch(e){}   // always fresh — track the wave list above
  const ordered = (typeof _sortedWavesDAG==='function') ? _sortedWavesDAG(WAVES) : (WAVES||[]);
  // preserve the operator's current selection across the rebuild (a re-plan may
  // drop the wave they had picked); fall back to the first wave otherwise.
  const prev = sel.value;
  sel.innerHTML = (ordered||[]).map(w=>`<option value="${attr(w.id)}">${esc(w.name)} (${esc(w.stage||'-')}, ${w.members.length})</option>`).join('') || '<option value="">no waves</option>';
  if(ordered.length){
    sel.value = ordered.some(w=>w.id===prev) ? prev : ordered[0].id;
  }
  lzGateCheck();
}

async function lzGateCheck(){
  const id = $('lzGateWave') && $('lzGateWave').value;
  const out = $('lzGateOut'); if(!out) return;
  if(!id){ out.innerHTML = '<span class="muted">select a wave</span>'; return; }
  out.innerHTML = '<span class="spinner"></span>';
  try{
    const g = await api('/waves/'+encodeURIComponent(id)+'/lz-gate');
    const mix = g.archetype_mix||{};
    const mixBadges = Object.keys(mix).filter(a=>mix[a]>0).map(a=>`${_lzArchBadge(a)} ${mix[a]}`).join(' ');
    const blk = g.blocking_archetypes||[];
    const verdict = g.ok ? '<span style="color:var(--green)">✓ can launch</span>' : '<span style="color:var(--red)">✗ blocked</span>';
    out.innerHTML = `<div class="row" style="gap:14px;align-items:baseline;flex-wrap:wrap">
        <span><span class="muted">verdict</span> <b>${verdict}</b></span>
        <span><span class="muted">archetype mix</span> ${mixBadges||'<span class="muted">—</span>'}</span>
      </div>
      <div class="muted" style="margin:4px 0">${esc(g.reason||'')}</div>
      ${blk.length?`<div class="ev-err" style="margin-top:4px">blocking: ${blk.map(_lzArchBadge).join(' ')}</div>`:''}`;
  }catch(e){ out.innerHTML = '<span class="ev-err">'+esc(String(e))+'</span>'; }
}

/* Section D — emit LZ IaC (scope=landing_zone, id=lz:<arch>) + show guardrails.
   Mirrors code.js showIac but self-contained so the operator can stand up the LZ
   Terraform without leaving the tab. */
async function lzEmitIac(a){
  try{
    await api('/iac-emit', {method:'POST', headers:{'content-type':'application/json'},
      body:JSON.stringify({scope:'landing_zone', scope_id:'lz:'+a, context:_LZ.archetypes[a]||{}})});
    toast('LZ IaC emit requested for '+a+' — artifact arrives via callback.', 'ok');
    setTimeout(()=>lzShowIac(a), 1800);
  }catch(e){ toast('lz iac-emit failed: '+e, 'err'); }
}

async function lzShowIac(a){
  const el = $('lzIac_'+a); if(!el) return;
  el.innerHTML = '<span class="spinner"></span>';
  try{
    const art = await api('/iac-artifacts/'+encodeURIComponent('lz:'+a));
    const gr = (art.guardrails||[]).map(g=>{
      const c = g.status==='pass'?'var(--green)':g.status==='fail'?'var(--red)':'var(--amber)';
      return `<tr><td style="color:${c};font-weight:600">${esc(g.status)}</td><td>${esc(g.pillar||'-')}</td><td>${esc(g.rule||'-')}</td><td>${esc(g.severity||'-')}</td></tr>`;
    }).join('') || '<tr><td colspan="4" class="muted">no guardrails</td></tr>';
    el.innerHTML = `<div class="muted" style="margin-bottom:2px">IaC artifact <b>lz:${a}</b> · ${art.guardrail_pass?'<span style="color:var(--green)">guardrails PASS</span>':'<span style="color:var(--red)">guardrails FAIL</span>'} · ${esc(art.scanned_at||'-')}</div>
      <div class="xscroll"><table class="tbl mcard"><thead><tr><th>status</th><th>pillar</th><th>rule</th><th>sev</th></tr></thead><tbody>${gr}</tbody></table></div>`;
  }catch(e){
    el.innerHTML = '<div class="muted" style="font-size:11px">no IaC artifact for lz:'+a+' yet — click "emit IaC" to generate (requires the executor).</div>';
  }
}

/* Section E (Phase B/C) — LLM LZ design interview + live preview + apply/activate.
   The interview endpoint drives LLMClient.design_lz (LLM proposes a blueprint
   dict, the deterministic validate_lz_design loops it until sound). The preview
   is NOT persisted; Apply posts it to /api/lz/designs (persist) then activates,
   Save draft persists without activating. Iterating passes the design_id so the
   endpoint loads the stored conversation as prior turns. */
let _LZ_DESIGN = null;     // the in-preview design dict from the last interview

async function loadLzDesigns(){
  const tb = $('lzDesignTbl') && $('lzDesignTbl').querySelector('tbody');
  if(!tb) return;
  tb.innerHTML = '';
  try{
    const data = await api('/lz/designs');
    const rows = (data.designs||[]).map(d=>{
      const active = d.is_active;
      const archs = (d.archetypes||[]).join(', ') || '-';
      const scaleTag = d.scale ? `<span class="tag">${esc(d.scale)}</span>` : '<span class="muted">—</span>';
      const desc = d.summary ? `<span class="muted" style="font-size:12px">${esc(d.summary)}</span>` : '<span class="muted">—</span>';
      return `<tr>
        <td data-label="name"><b>${esc(d.name||'-')}</b> <span class="muted" style="font-size:11px">${esc(d.design_id||'')}</span></td>
        <td data-label="scale">${scaleTag}</td>
        <td data-label="archetypes">${esc(archs)}</td>
        <td data-label="description">${desc}</td>
        <td data-label="active">${active?'<span class="tag" style="color:var(--green)">active</span>':'<span class="muted">—</span>'}</td>
        <td data-label="updated"><span class="muted">${esc(d.updated_at||'-')}</span></td>
        <td data-label="actions">
          ${active?'':`<button class="sm primary" onclick="activateDesign('${attr(d.design_id)}')">activate</button>`}
          <button class="sm" onclick="continueDesign('${attr(d.design_id)}')">continue</button>
          ${active?'':`<button class="sm" onclick="deleteDesign('${attr(d.design_id)}')">delete</button>`}
        </td></tr>`;
    }).join('');
    tb.innerHTML = rows || '<tr><td colspan="7" class="muted">no saved designs — the built-in corp/online/dmz is the default. Interview MigraQ above to author one.</td></tr>';
  }catch(e){ tb.innerHTML = `<tr><td colspan="7" class="ev-err">${esc(e)}</td></tr>`; }
}

async function designLz(designId){
  const demand = ($('lzDesignDemand').value||'').trim();
  if(!demand){ toast('describe the landing zone you need first','warn'); return; }
  const onprem = ($('lzDesignOnprem').value||'').trim()
    .split(',').map(s=>s.trim()).filter(Boolean);
  const out = $('lzDesignOut'), pv = $('lzDesignPreview');
  out.innerHTML = '<span class="spinner"></span> interviewing MigraQ…';
  pv.innerHTML = '';
  $('lzDesignApplyBtn').disabled = $('lzDesignSaveBtn').disabled = true;
  // continue an existing design's conversation when an id is given, or the one
  // held in-preview from a prior interview this session
  const cont = designId || (_LZ_DESIGN && _LZ_DESIGN.design_id) || null;
  try{
    const body = {demand};
    if(cont) body.design_id = cont;
    if(onprem.length) body.onprem_cidrs = onprem;
    if(_lzScale) body.scale = _lzScale;
    const r = await api('/lz/design/interview', {method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    _LZ_DESIGN = r.design || null;
    out.innerHTML = _lzDesignStatus(r);
    pv.innerHTML = _lzDesignPreview(r);
    // enable Apply only when the proposal passed validation
    const ok = r.ok && r.design && (r.validation||{}).ok;
    $('lzDesignApplyBtn').disabled = !ok;
    $('lzDesignSaveBtn').disabled = !(r.design && (r.design.archetypes||{}).length);
    if(r.ok) toast('MigraQ proposed a validated LZ design — preview it, then Apply.','ok');
    else if(r.design) toast('MigraQ proposal did not validate — see the errors; iterate to fix.','warn');
    else toast('MigraQ unavailable: '+(r.error||''),'err');
  }catch(e){
    out.innerHTML = '<span class="ev-err">interview failed: '+esc(e)+'</span>';
  }
}

function _lzDesignStatus(r){
  const rounds = r.rounds||[];
  const ok = r.ok;
  const badge = ok ? '<span style="color:var(--green)">✓ validated</span>'
                   : '<span style="color:var(--red)">✗ not validated</span>';
  const errList = (r.validation && r.validation.errors && r.validation.errors.length)
    ? `<div class="ev-err" style="margin-top:4px">${r.validation.errors.map(esc).join('; ')}</div>` : '';
  const roundTrail = rounds.length
    ? `<div class="muted" style="font-size:11px;margin-top:4px">${rounds.length} round(s): ${rounds.map(x=>x.ok?'✓':'✗ '+ (x.errors||[]).length+' err').join(' → ')}</div>` : '';
  return `<div class="row" style="gap:10px;align-items:baseline;flex-wrap:wrap">
      <span><span class="muted">verdict</span> <b>${badge}</b></span>
      ${r.error?`<span class="muted">${esc(r.error)}</span>`:''}
    </div>${errList}${roundTrail}`;
}

function _lzDesignPreview(r){
  const d = r.design; if(!d) return '';
  const archs = d.archetypes||{};
  const req = d.requirements||{};
  const clf = req.classifier||{};
  const gov = req.governance||{};
  const scaleBadge = d.scale ? `<span class="tag" style="color:var(--fg)">${esc(d.scale)} scale</span>` : '';
  const clfChip = (clf.tag_map&&Object.keys(clf.tag_map).length)||clf.default
    ? '<span class="tag" style="color:var(--green)">design-aware classification</span>' : '';
  // governance summary: identity / audit / network golden-rule defaults the
  // scale filled (the operator can override any via the prompt).
  const govChip = gov.identity ? `<details style="margin-top:4px"><summary class="muted" style="cursor:pointer;font-size:12px">governance (scale defaults — identity / audit / network)</summary>
    <div style="font-size:11px;margin-top:4px">
      <div><b>identity:</b> ${esc((gov.identity||{}).rule||'-')}</div>
      <div><b>audit:</b> ${esc((gov.audit||{}).rule||'-')} → ${esc((gov.audit||{}).destination_account||'-')} account</div>
      <div><b>network:</b> ${esc((gov.network||{}).planning||'-')}${(gov.network||{}).cfw_inspection?' · CFW inspection':''}</div>
      <div><b>budgeting:</b> ${esc(gov.budgeting||'-')}</div>
    </div></details>` : '';
  const cards = Object.keys(archs).map(name=>{
    const bp = archs[name]||{};
    const vpc = bp.vpc||{}, peer = bp.peering||{}, eg = bp.internet_egress||{};
    const peerBits = Object.keys(peer).filter(k=>peer[k]).map(k=>`${esc(k.replace('to_','→ '))}`).join(', ')||'<span class="muted">none</span>';
    const egBits = ['nat','public_clb','cdn','waf'].filter(k=>eg[k]!==undefined).map(k=>`${k}:${eg[k]?'✓':'✗'}`).join(' · ');
    const pol = bp.policy_as_code||[];
    return `<div class="mcard" style="padding:10px;margin-top:6px;font-size:12px">
      <div><b>${esc(name)}</b> <span class="muted">${esc(bp.summary||'')}</span></div>
      <div class="grid2col" style="margin-top:4px">
        <div><span class="muted">VPC</span><br><b>${esc(vpc.name||'-')}</b> <span class="muted">${esc(vpc.cidr||'')}</span></div>
        <div><span class="muted">peering</span><br>${peerBits}</div>
        <div><span class="muted">egress</span><br>${esc(egBits||'-')}</div>
        <div><span class="muted">policy</span><br>${pol.length} rule(s)</div>
      </div></div>`;
  }).join('');
  const conv = (d.conversation||[]).map((t,i)=>{
    const who = t.role==='user'?'you':'MigraQ';
    // the assistant turn is raw JSON — show a short head, not the whole blob
    const c = t.role==='assistant' ? (t.content||'').slice(0,160)+'…' : (t.content||'');
    return `<div style="margin-top:4px"><span class="muted" style="font-weight:600">${who}:</span> <span style="white-space:pre-wrap">${esc(c)}</span></div>`;
  }).join('');
  const sumLine = d.summary ? `<div class="muted" style="font-size:12px;margin-top:4px"><b>AI description:</b> ${esc(d.summary)}</div>` : '';
  return `<div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
      <span class="muted">proposed design</span> <b>${esc(d.name||'-')}</b>
      ${scaleBadge}
      <span class="muted">${Object.keys(archs).length} archetype(s)</span>
      ${clfChip}
      ${(d.onprem_cidrs||[]).length?`<span class="muted">on-prem CIDRs: ${esc((d.onprem_cidrs||[]).join(', '))}</span>`:''}
    </div>
    ${sumLine}
    ${cards}
    ${govChip}
    ${conv?`<details style="margin-top:6px"><summary class="muted" style="cursor:pointer">conversation (${(d.conversation||[]).length} turns)</summary><div style="margin-top:4px;font-size:12px">${conv}</div></details>`:''}`;
}

async function applyDesign(opts){
  if(!_LZ_DESIGN || !_LZ_DESIGN.archetypes){ toast('no design in preview — interview MigraQ first','warn'); return; }
  const activate = !(opts && opts.activate===false);
  const d = _LZ_DESIGN;
  const body = {name:d.name, summary:d.summary||'', archetypes:d.archetypes,
                requirements:d.requirements, conversation:d.conversation,
                onprem_cidrs:d.onprem_cidrs, updated_by:'operator',
                design_id:d.design_id, scale:d.scale||_lzScale||''};
  try{
    const cr = await api('/lz/designs', {method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    if(!cr || !cr.design_id){ throw new Error('persist rejected the design (invalid?)'); }
    if(activate){
      await api('/lz/designs/'+encodeURIComponent(cr.design_id)+'/activate',
        {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({by:'operator'})});
      toast('LZ design applied & activated — archetypes/gate now follow it.','ok');
    } else {
      toast('LZ design saved as a draft (not activated).','ok');
    }
    _LZ_DESIGN.design_id = cr.design_id;   // keep continuity for further iterate
    await loadLzArchetypes();
    await loadLzDesigns();
    loadLzClassification();
  }catch(e){ toast('apply failed: '+e,'err'); }
}

async function activateDesign(designId){
  try{
    await api('/lz/designs/'+encodeURIComponent(designId)+'/activate',
      {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({by:'operator'})});
    toast('design activated.','ok');
    await loadLzArchetypes(); await loadLzDesigns(); loadLzClassification();
  }catch(e){ toast('activate failed: '+e,'err'); }
}

async function deleteDesign(designId){
  if(!confirm('delete this LZ design?')) return;
  try{
    await api('/lz/designs/'+encodeURIComponent(designId), {method:'DELETE'});
    if(_LZ_DESIGN && _LZ_DESIGN.design_id===designId) _LZ_DESIGN = null;
    toast('design deleted.','ok');
    await loadLzDesigns();
  }catch(e){ toast('delete failed: '+e,'err'); }
}

async function continueDesign(designId){
  // load the stored design so the operator can iterate it; put its name into the
  // demand box as a prompt and stash its id so the next Design call continues it
  try{
    const d = await api('/lz/designs/'+encodeURIComponent(designId));
    _LZ_DESIGN = d;            // design_id + conversation now in-preview
    $('lzDesignDemand').value = '';   // operator types the follow-up
    $('lzDesignDemand').placeholder = `iterate "${d.name||''}" — e.g. "add a pci tier" or "make the hub CIDR 10.8.0.0/16"`;
    $('lzDesignOut').innerHTML = `<span class="muted">Continuing <b>${esc(d.name||'')}</b> — type the change and click "Design with MigraQ".</span>`;
    $('lzDesignPreview').innerHTML = _lzDesignPreview({design:d});
    toast('loaded design for iteration','info');
  }catch(e){ toast('load failed: '+e,'err'); }
}

/* Section F (Phase B+) — Network designer: VPC + subnets per account + placement.
   Generates the topology (carve each active-LZ-design archetype VPC into web/app/
   data/infra subnets) and places every server into a VPC + subnet + IP. MigraQ may
   propose the carving policy; otherwise the deterministic default is used. The
   per-server placement table itself lives in the merged "Server placement" card
   (loadLzClassification) — click a row there to move a server to a different
   account / VPC / subnet (a pin that survives regenerate). Apply persists +
   activates the design so overrides stick. */
let _ND = null;     // the active network design (or last preview)

async function loadNetworkDesigner(){
  // render the active design (if any) + the saved-designs list
  renderNdSaved();
  try{
    const r = await api('/lz/network/design');
    _ND = r.active || null;
    renderNdTopo(_ND);
  }catch(e){
    $('ndTopo').innerHTML = '<span class="muted">no active network design — click "Generate &amp; apply" to build one.</span>';
  }
}

async function generateNetwork(opts){
  const activate = !(opts && opts.activate===false);
  const demand = ($('ndDemand').value||'').trim();
  const pfx = ($('ndPrefix').value||'').trim();
  const out = $('ndOut'); out.innerHTML = '<span class="spinner"></span> '+(activate?'generating & applying…':'generating preview…');
  const body = {activate, by:'operator'};
  if(demand) body.demand = demand;
  if(pfx && /^\d+$/.test(pfx)) body.policy = {subnet_prefix: parseInt(pfx,10)};
  try{
    const r = await api('/lz/network/design', {method:'POST',
      headers:{'content-type':'application/json'}, body:JSON.stringify(body)});
    _ND = r.design || null;
    out.innerHTML = _ndStatus(r);
    renderNdTopo(_ND);
    renderNdSaved();
    // re-fetch /lz/archetypes so _LZ.per_server picks up the freshly-generated
    // placements (the table reads per_server, not _ND directly) — otherwise it
    // would show the pre-generate placements until a manual Refresh.
    await loadLzArchetypes();
    loadLzClassification();   // keep the merged Server placement table in sync
    if(r.ok) toast(activate?'Network design generated & applied.':'Network design previewed (not applied).','ok');
    else toast('Network design has validation errors — see below.','warn');
  }catch(e){ out.innerHTML = '<span class="ev-err">generate failed: '+esc(e)+'</span>'; }
}

function _ndStatus(r){
  const d = r.design||{}; const v = d.validation||{};
  const badge = v.ok ? '<span style="color:var(--green)">✓ valid</span>'
                     : '<span style="color:var(--red)">✗ '+ (v.errors||[]).length +' error(s)</span>';
  const rounds = r.rounds||[];
  const roundTrail = rounds.length ? `<span class="muted" style="font-size:11px"> · policy ${rounds.length} round(s): ${rounds.map(x=>x.ok?'✓':'✗').join('→')}</span>` : '';
  const errs = (v.errors||[]).length
    ? `<details style="margin-top:4px"><summary class="ev-err" style="cursor:pointer">${v.errors.length} validation error(s)</summary><ul style="margin:4px 0 0 18px">${v.errors.map(e=>`<li>${esc(e)}</li>`).join('')}</ul></details>` : '';
  const placed = Object.keys(d.placements||{}).length;
  const managed = Object.values(d.placements||{}).filter(p=>p.managed).length;
  const sumLine = d.summary ? `<div class="muted" style="font-size:12px;margin-top:4px"><b>AI description:</b> ${esc(d.summary)}</div>` : '';
  return `<div class="row" style="gap:10px;align-items:baseline;flex-wrap:wrap">
      <span><span class="muted">verdict</span> <b>${badge}</b></span>
      <span class="muted">${(d.vpcs||[]).length} VPC(s) · ${placed} server(s) placed${managed?` · ${managed} PaaS (no VPC IP — excluded from subnet sizing)`:''}</span>
      ${r.active?'<span class="tag" style="color:var(--green)">active</span>':'<span class="muted">preview</span>'}
      ${roundTrail}
    </div>${sumLine}${errs}`;
}

/* tier colors for the topology bar (separate from the LZ archetype colors) */
const _TIER_COLOR = {web:'#3b8eea', app:'var(--green)', data:'#a06bff', infra:'#888'};
function _cidrSize(cidr){ const p=parseInt((cidr||'').split('/')[1],10); return isFinite(p)&&p<=32 ? Math.pow(2,32-p) : 1; }

function renderNdTopo(nd){
  const el = $('ndTopo'); if(!el) return;
  if(!nd || !nd.vpcs || !nd.vpcs.length){ el.innerHTML = '<span class="muted">no topology — generate a design to see the VPC + subnet layout.</span>'; return; }
  // group VPCs by account so the topology reads account -> VPC -> subnets
  const byAcct = {};
  nd.vpcs.forEach(v=>{ (byAcct[v.account] = byAcct[v.account]||[]).push(v); });
  const archs = _LZ.archetypes || {};
  const legend = ['web','app','data','infra'].map(t=>
    `<span class="tag" style="color:${_TIER_COLOR[t]}">■ ${t}</span>`).join(' ');
  const cards = Object.entries(byAcct).map(([acct, vpcs])=>{
    const vpcHtml = vpcs.map(v=>{
      const subs = v.subnets||[];
      const total = subs.reduce((a,s)=>a+_cidrSize(s.cidr),0) || 1;
      // peering edges from the active LZ design's blueprint (to_<other>=true)
      const peer = (archs[v.archetype]||{}).peering || {};
      const peers = Object.keys(peer).filter(k=>k.indexOf('to_')===0 && peer[k])
                        .map(k=>k.replace('to_','')).join(', ');
      // address-space bar: each subnet is a block sized by its address count
      const bar = subs.map(s=>{
        const sz = _cidrSize(s.cidr);
        const flex = Math.max(0.5, sz/total*100).toFixed(2);
        const cap = s.usable||s.capacity||0, used = s.used||0;
        const fillPct = cap ? Math.min(100, Math.round(used/cap*100)) : 0;
        const pfx = (s.cidr||'').split('/')[1] || '';
        return `<div title="${esc(s.name)} ${esc(s.cidr)} · tier ${esc(s.tier||'-')} · used ${used}/${cap} · gw ${esc(s.gateway||'-')}"
          style="flex:${flex} 0 0;min-width:54px;background:${_TIER_COLOR[s.tier]||'#888'};position:relative;border-radius:5px;padding:5px;color:#fff;font-size:11px;overflow:hidden;min-height:54px">
          <div style="position:absolute;left:0;bottom:0;height:${fillPct}%;width:100%;background:rgba(0,0,0,.35)"></div>
          <div style="position:relative"><b>${esc(s.name)}</b><br>/${esc(pfx)}<br><span style="opacity:.92">${used}/${cap}</span></div>
        </div>`;
      }).join('');
      const tbl = `<table class="tbl" style="margin-top:6px;font-size:11px"><thead><tr>
        <th>subnet</th><th>cidr</th><th>tier</th><th>used / cap</th><th>gateway</th></tr></thead><tbody>
        ${subs.map(s=>`<tr><td><b>${esc(s.name)}</b></td><td>${esc(s.cidr)}</td>
          <td style="color:${_TIER_COLOR[s.tier]||'#888'}">${esc(s.tier||'-')}</td>
          <td>${s.used||0} / ${s.usable||s.capacity||0}</td><td class="muted">${esc(s.gateway||'-')}</td></tr>`).join('')}
        </tbody></table>`;
      return `<div class="mcard" style="padding:10px;margin-top:8px">
        <div class="row" style="justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px">
          <div><b>${esc(v.name||v.archetype)}</b> <span class="tag" style="color:var(--fg)">${esc(v.archetype)}</span>
            <span class="muted">${esc(v.cidr||'-')}</span></div>
          <div class="muted" style="font-size:11px">${peers?('peers → '+esc(peers)):'no peering'}</div>
        </div>
        <div class="row" style="gap:3px;margin-top:6px;align-items:stretch">${bar||'<span class="muted">no subnets</span>'}</div>
        ${tbl}
      </div>`;
    }).join('');
    return `<div style="margin-top:6px"><div class="muted" style="font-weight:600;margin-top:6px">Account: ${esc(acct)} <span class="muted" style="font-weight:400">· ${vpcs.length} VPC(s)</span></div>${vpcHtml}</div>`;
  }).join('');
  const errBadge = (nd.validation||{}).ok ? '' : ` <span class="tag" style="color:var(--red)" title="${esc(((nd.validation||{}).errors||[]).join('; '))}">${((nd.validation||{}).errors||[]).length} error(s)</span>`;
  el.innerHTML = `<div class="row" style="gap:8px;align-items:center;margin-bottom:2px;flex-wrap:wrap">
      <b>VPC &amp; subnet design</b> <span class="muted">· ${nd.vpcs.length} VPC(s) across ${Object.keys(byAcct).length} account(s) · one subnet per tier</span>
      ${errBadge}
    </div>
    <div class="row" style="gap:8px;align-items:center;margin-bottom:2px;flex-wrap:wrap"><span class="muted" style="font-size:11px">tier legend:</span>${legend}
      <span class="muted" style="font-size:11px">· block size = subnet address space · dark fill = used capacity</span></div>
    ${cards}`;
}

async function renderNdSaved(){
  const tb = $('ndSavedTbl') && $('ndSavedTbl').querySelector('tbody'); if(!tb) return;
  try{
    const data = await api('/lz/network/designs');
    const rows = (data.designs||[]).map(d=>{
      const desc = d.summary ? `<span class="muted" style="font-size:12px">${esc(d.summary)}</span>` : '<span class="muted">—</span>';
      return `<tr>
        <td data-label="name"><b>${esc(d.name||'-')}</b> <span class="muted" style="font-size:11px">${esc(d.design_id||'')}</span></td>
        <td data-label="description">${desc}</td>
        <td data-label="vpcs">${d.vpc_count||0}</td>
        <td data-label="placed">${d.placement_count||0}</td>
        <td data-label="valid">${d.valid?'<span style="color:var(--green)">✓</span>':'<span style="color:var(--red)">✗</span>'}</td>
        <td data-label="active">${d.is_active?'<span class="tag" style="color:var(--green)">active</span>':'<span class="muted">—</span>'}</td>
        <td data-label="actions">
          ${d.is_active?'':`<button class="sm primary" onclick="activateNd('${attr(d.design_id)}')">activate</button>`}
          ${d.is_active?'':`<button class="sm" onclick="deleteNd('${attr(d.design_id)}')">delete</button>`}
        </td></tr>`;
    }).join('');
    tb.innerHTML = rows || '<tr><td colspan="7" class="muted">no saved network designs.</td></tr>';
  }catch(e){ tb.innerHTML = `<tr><td colspan="7" class="ev-err">${esc(e)}</td></tr>`; }
}

async function activateNd(designId){
  try{
    await api('/lz/network/designs/'+encodeURIComponent(designId)+'/activate',
      {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({by:'operator'})});
    toast('network design activated.','ok'); await loadNetworkDesigner();
  }catch(e){ toast('activate failed: '+e,'err'); }
}

async function deleteNd(designId){
  if(!confirm('delete this network design?')) return;
  try{
    await api('/lz/network/designs/'+encodeURIComponent(designId), {method:'DELETE'});
    if(_ND && _ND.design_id===designId) _ND = null;
    toast('deleted.','ok'); await loadNetworkDesigner();
  }catch(e){ toast('delete failed: '+e,'err'); }
}