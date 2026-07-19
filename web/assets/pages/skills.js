/* Skills tab — manage the LLM skills that drive 7R / audit / wave-runbook /
   plan-review / LZ / network / waveplan. Three panes:
     LEFT   catalog — grouped Built-in/Custom + search + new-from-template
     MIDDLE editor  — config FORM (no raw YAML) + prompt-body textarea + token
                      meter + Try / Save / Diff / Undo / Reset
     RIGHT  context — I/O contract (fires/takes/returns) + bundled Resources
                      (with "used in last run ✓" badges) + Last-run output

   Design notes (AI-expert): config is typed (dropdowns/numbers) so it can't be
   typo-broken; the prompt body is the only prose. Try runs the CURRENT
   (unsaved) prompt on free-text input and shows the parsed output + which
   bundled resources the agent touched — so you preview AI-behavior changes
   before committing. Diff shows what you changed vs the built-in default.
   Undo restores the previous save (one-step, .prev snapshot). Wired skills
   (seven_r/audit/assess/review) take structured input → Try points to the
   Inventory drawer instead of free text. */
let _skCurrent = null;     // current skill name (null = none loaded)
let _skSkills = null;      // cached GET /api/skills list
let _skView = null;        // cached skill_view for the loaded skill
let _skEditable = false;   // any skill editable (overlay dir configured)
let _skFilePath = null;    // currently-open bundled file path
let _skLastTouched = null; // resource paths touched in the last Try (for badges)
const _WIRED = ["seven_r", "audit_match", "assess_wave", "review_plan"];

// ---------------------------------------------------------------------------
// catalog (left pane)
// ---------------------------------------------------------------------------
async function loadSkills(){
  try{
    const skills = await api('/skills');
    _skSkills = skills;
    _skEditable = skills.some(s=>s.editable);
    renderSkillCatalog();
    const banner = $('skillsBanner');
    if(!_skEditable){
      banner.style.display = 'block';
      banner.innerHTML = '<span style="color:#b45309">Editing is disabled.</span> ' +
        '<span class="muted">Set <span class="kbd">IDC_SKILLS_DIR</span> to a writable directory (in .env / systemd drop-in) and restart the backend to enable editing skills here.</span>';
    }else{
      banner.style.display = 'none';
    }
  }catch(e){
    $('skillsList').innerHTML = '<span class="ev-err">failed to load: '+esc(String(e))+'</span>';
  }
}

function renderSkillCatalog(){
  const q = ($('skSearch').value || '').toLowerCase().trim();
  const skills = (_skSkills || []).filter(s=>
    !q || s.name.toLowerCase().includes(q) || (s.description||'').toLowerCase().includes(q));
  const builtin = skills.filter(s=>s.overriding_default);
  const custom  = skills.filter(s=>!s.overriding_default);
  const srcMark = s => s.source==='overlay' ? '●' : s.source==='builtin' ? '○' : '·';
  const row = s=>{
    const c = s.backend==='codex' ? '#3b82f6' : '#16a34a';
    return `<div class="row" style="padding:6px 0;border-bottom:1px solid var(--border);align-items:center;gap:8px;cursor:pointer" onclick="editSkill('${esc(s.name)}')">
      <span style="color:var(--muted);font-size:10px;width:10px;text-align:center">${srcMark(s)}</span>
      <div style="flex:1;min-width:0">
        <div><b style="font-size:12px">${esc(s.name)}</b>
          <span style="font-size:9px;color:#fff;background:${c};padding:1px 5px;border-radius:8px">${esc(s.backend)}</span>
          ${s.files && s.files.length ? `<span style="font-size:9px;color:var(--muted)">📎${s.files.length}</span>` : ''}
        </div>
        <div class="muted" style="font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.description||'')}</div>
      </div>
    </div>`;
  };
  let html = '';
  if(builtin.length){
    html += `<div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin:4px 0">built-in (${builtin.length})</div>`;
    html += builtin.map(row).join('');
  }
  if(custom.length){
    html += `<div class="muted" style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin:10px 0 4px">custom (${custom.length})</div>`;
    html += custom.map(row).join('');
  }
  if(!skills.length) html = '<span class="muted">no matches</span>';
  $('skillsList').innerHTML = html;
}

// ---------------------------------------------------------------------------
// editor (middle pane)
// ---------------------------------------------------------------------------
async function editSkill(name){
  _skCurrent = name;
  _skLastTouched = null;
  try{
    const s = await api('/skills/'+encodeURIComponent(name));
    _skView = s;
    _populateEditor(s);
  }catch(e){
    toast('load skill failed: '+e, 'err');
  }
}

function _populateEditor(s){
  $('skName').textContent = s.name;
  $('skBadge').innerHTML = `<span style="font-size:11px" class="muted">${esc(s.backend)} · ${esc(s.source||'')}</span>`;
  $('skDesc').value       = s.description || '';
  $('skBackend').value    = s.backend || 'chat';
  $('skOutput').value     = s.output || 'json';
  $('skMode').value       = s.mode || 'plan';
  $('skRounds').value     = s.max_rounds || 1;
  $('skModel').value      = s.model || '';
  $('skValidator').value  = s.validator || '';
  $('skEditor').value     = s.system || '';
  onBackendChange();
  updateTokenMeter();
  const editable = !!s.editable;
  ['skDesc','skBackend','skOutput','skMode','skRounds','skModel','skValidator','skEditor']
    .forEach(id=>{ $(id).disabled = !editable; });
  $('skConfig').style.display = 'block';

  // production-edit warning
  const warn = $('skWarn');
  if(editable && s.overriding_default && s.used_by){
    warn.style.display = 'block';
    warn.innerHTML = `<b>⚠ editing a production skill</b> — this changes a live path (${esc(s.used_by)}). Use <b>Try</b> before Save; <b>Undo</b> reverts one save.`;
  }else{
    warn.style.display = 'none';
  }

  // buttons
  const wired = _WIRED.includes(s.name);
  $('skSaveBtn').disabled  = !editable;
  $('skResetBtn').disabled = !(editable && s.source==='overlay');
  $('skDiffBtn').disabled  = !s.default_body;
  $('skTryBtn').disabled   = wired;
  $('skTryBtn').textContent = wired ? 'Try (use drawer)' : 'Try…';
  $('skStatus').textContent = '';
  $('skDiff').style.display = 'none';
  closeTry();
  const hint = s.overriding_default
    ? "Has a built-in default (the _FALLBACK constant). Reset removes your overlay so that default takes over."
    : "No built-in default — Reset removes your overlay (the skill then won't resolve unless re-created).";
  $('skDefaultHint').textContent = hint;

  renderIOCard(s, wired);
  _renderSkillFiles(s);

  // undo: enabled only if a .prev snapshot exists (checked async)
  $('skUndoBtn').disabled = true;
  if(editable && s.source==='overlay'){
    api('/skills/'+encodeURIComponent(s.name)+'/prev').then(r=>{
      $('skUndoBtn').disabled = !r.has_prev;
    }).catch(()=>{ $('skUndoBtn').disabled = true; });
  }

  $('skLastRun').style.display = 'none';
}

function onBackendChange(){
  const codex = $('skBackend').value === 'codex';
  $('skModeWrap').style.display = codex ? 'block' : 'none';
}

function updateTokenMeter(){
  const len = ($('skEditor').value || '').length;
  const tok = Math.ceil(len/4);   // rough char→token estimate (prompt budget)
  const color = tok<1500 ? 'var(--green)' : tok<3000 ? 'var(--amber)' : 'var(--red)';
  const pct = Math.min(100, tok/40);
  $('skTokMeter').innerHTML =
    `<span style="color:${color}">≈${tok} tok</span> ` +
    `<span style="display:inline-block;width:60px;height:4px;background:var(--panel2);border-radius:2px;vertical-align:middle">` +
    `<span style="display:block;height:4px;width:${pct}%;background:${color};border-radius:2px"></span></span>`;
}

// build the --- frontmatter --- + body from the form fields + textarea
function reconstructMarkdown(){
  const name = _skCurrent || '';
  const desc = $('skDesc').value.trim();
  const backend = $('skBackend').value;
  const output = $('skOutput').value;
  const mode = $('skMode').value;
  const rounds = parseInt($('skRounds').value, 10) || 1;
  const model = $('skModel').value.trim();
  const validator = $('skValidator').value.trim();
  const body = $('skEditor').value;
  const lines = ['---', `name: ${name}`];
  lines.push(`description: "${(desc||'').replace(/"/g,'\\"')}"`);
  lines.push(`backend: ${backend}`);
  if(backend==='codex') lines.push(`mode: ${mode}`);
  lines.push(`output: ${output}`);
  if(rounds !== 1) lines.push(`max_rounds: ${rounds}`);
  if(model) lines.push(`model: ${model}`);
  if(validator) lines.push(`validator: ${validator}`);
  lines.push('---', '', body);
  return lines.join('\n');
}

async function saveSkill(){
  if(!_skCurrent) return;
  if(!$('skEditor').value.trim()){ toast('prompt body empty','err'); return; }
  $('skStatus').textContent = 'saving…';
  try{
    const md = reconstructMarkdown();
    await api('/skills/'+encodeURIComponent(_skCurrent), {
      method:'PUT', headers:{'content-type':'application/json'},
      body: JSON.stringify({markdown: md})});
    $('skStatus').textContent = 'saved — takes effect immediately';
    toast('skill saved','ok');
    await loadSkills();
    await editSkill(_skCurrent);
  }catch(e){
    $('skStatus').textContent = '';
    toast('save failed: '+e, 'err');
  }
}

async function resetSkill(){
  if(!_skCurrent) return;
  if(!confirm('Reset "'+_skCurrent+'" — remove your overlay so the built-in default takes over?')) return;
  try{
    await api('/skills/'+encodeURIComponent(_skCurrent), {method:'DELETE'});
    toast('reset to default','ok');
    await loadSkills();
    await editSkill(_skCurrent);
  }catch(e){
    toast('reset failed: '+e, 'err');
  }
}

async function undoSkill(){
  if(!_skCurrent) return;
  if(!confirm('Undo last save — restore the previous prompt?')) return;
  try{
    const r = await api('/skills/'+encodeURIComponent(_skCurrent)+'/undo', {method:'POST'});
    if(!r.restored){ toast('no previous save to undo','err'); return; }
    toast('undone','ok');
    await loadSkills();
    await editSkill(_skCurrent);
  }catch(e){
    toast('undo failed: '+e, 'err');
  }
}

// --- diff vs default ------------------------------------------------------
function toggleDiff(){
  const d = $('skDiff');
  if(d.style.display === 'block'){ d.style.display = 'none'; return; }
  if(!_skView || !_skView.default_body){ toast('no default to diff against','err'); return; }
  d.innerHTML = renderDiff(_skView.default_body, $('skEditor').value);
  d.style.display = 'block';
}

function renderDiff(a, b){
  const d = simpleDiff(a, b);
  const lines = d.filter(x=>x.t!=='eq').map(x=>{
    const c = x.t==='add' ? 'var(--green)' : 'var(--red)';
    const p = x.t==='add' ? '+' : '−';
    return `<span style="color:${c}">${p} ${esc(x.s)}</span>`;
  });
  return lines.join('\n') || '<span class="muted">no changes from the default</span>';
}

// line-level LCS diff → [{t:'eq'|'add'|'del', s:line}]. a=default, b=current.
function simpleDiff(a, b){
  const A = (a||'').split('\n'), B = (b||'').split('\n');
  const n = A.length, m = B.length;
  const dp = Array.from({length:n+1}, ()=>new Array(m+1).fill(0));
  for(let i=n-1;i>=0;i--) for(let j=m-1;j>=0;j--)
    dp[i][j] = A[i]===B[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);
  const out = [];
  let i=0, j=0;
  while(i<n && j<m){
    if(A[i]===B[j]){ out.push({t:'eq', s:A[i]}); i++; j++; }
    else if(dp[i+1][j] >= dp[i][j+1]){ out.push({t:'del', s:A[i]}); i++; }
    else { out.push({t:'add', s:B[j]}); j++; }
  }
  while(i<n) out.push({t:'del', s:A[i++]});
  while(j<m) out.push({t:'add', s:B[j++]});
  return out;
}

// --- new skill from template ----------------------------------------------
function newSkill(template){
  if(!_skEditable){ toast('editing disabled — set IDC_SKILLS_DIR','err'); return; }
  const name = prompt('New skill name (lowercase, _ or -, e.g. my_audit):',
                      template==='chat' ? 'my_skill' : 'my_code_skill');
  if(!name) return;
  if(!/^[a-z0-9_-]+$/.test(name)){ toast('name: lowercase letters, digits, _ , - only','err'); return; }
  const scaffolds = {
    chat: {backend:'chat',  body:`You are a cloud-migration assistant.\n\nReply ONLY with a JSON object.\n`},
    codex:{backend:'codex', body:`You are a code-grounded migration agent.\n\nInspect the repo, decide, then reply ONLY with a JSON object.\n`},
    codex_resources:{backend:'codex', body:`You are a code-grounded migration agent.\n\nRead the bundled references under references/ first, then inspect the repo. Run scripts/ only in execute mode. Reply ONLY with a JSON object.\n`},
  };
  const t = scaffolds[template] || scaffolds.chat;
  const s = {name, description:'', backend:t.backend, output:'json', mode:'plan',
    max_rounds:1, model:'', validator:'', system:t.body, files:[], source:'new',
    editable:true, overriding_default:false, default_body:'', used_by:'', schema:{}};
  _skCurrent = name;
  _skView = s;
  _populateEditor(s);
  toast('scaffold loaded — edit then Save to create','ok');
}

// --- try panel ------------------------------------------------------------
function trySkill(){
  if(_WIRED.includes(_skCurrent)){
    toast('This skill takes a host/app — try it from the Inventory drawer','err');
    return;
  }
  $('skTryPanel').style.display = 'block';
  $('skTryOut').style.display = 'none';
  $('skTryStatus').textContent = '';
  $('skTryInput').focus();
}

function closeTry(){ $('skTryPanel').style.display = 'none'; }

async function runTry(){
  if(!_skCurrent) return;
  const input = $('skTryInput').value;
  const md = reconstructMarkdown();   // try the CURRENT (unsaved) prompt
  $('skTryStatus').textContent = 'running…';
  $('skTryOut').style.display = 'none';
  try{
    const r = await api('/skills/'+encodeURIComponent(_skCurrent)+'/try', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({input, markdown: md})});
    $('skTryStatus').textContent = r.ok ? 'done' : ('failed: '+(r.error||r.kind));
    const out = r.output;
    const shown = (out && typeof out==='object') ? JSON.stringify(out, null, 2) : (out || '');
    $('skTryOut').textContent = shown || (r.error || '(no output)');
    $('skTryOut').style.display = 'block';
    if(r.ok){
      _skLastTouched = r.resources_touched || [];
      $('skLastRun').style.display = 'block';
      $('skLastRunBody').innerHTML =
        `<div>kind: <b>${esc(r.kind)}</b></div>` +
        `<pre style="margin:4px 0;max-height:200px;overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px;font-size:11px;white-space:pre-wrap">${esc(shown)}</pre>` +
        (r.resources_touched && r.resources_touched.length
          ? `<div class="muted" style="font-size:11px">resources used: ${r.resources_touched.map(esc).join(', ')}</div>` : '');
      if(_skView) _renderSkillFiles(_skView);   // refresh "✓ used" badges
    }
  }catch(e){
    $('skTryStatus').textContent = '';
    $('skTryOut').textContent = String(e);
    $('skTryOut').style.display = 'block';
  }
}

// ---------------------------------------------------------------------------
// context (right pane): I/O card + bundled resources
// ---------------------------------------------------------------------------
function renderIOCard(s, wired){
  const parts = [];
  if(s.used_by)      parts.push(`<div><span class="muted">fires:</span> ${esc(s.used_by)}</div>`);
  if(s.description)  parts.push(`<div><span class="muted">when:</span> ${esc(s.description)}</div>`);
  const sch = s.schema || {};
  const props = sch.properties || sch;
  if(props && typeof props==='object' && Object.keys(props).length){
    const keys = Object.keys(props).map(k=>`<code style="font-size:11px">${esc(k)}</code>`).join(' ');
    parts.push(`<div><span class="muted">returns:</span> ${keys}</div>`);
  }
  if(wired){
    parts.push(`<div style="margin-top:6px;color:var(--amber)">structured input — try via the Inventory drawer's AI buttons, not free text.</div>`);
  }else{
    parts.push(`<div style="margin-top:6px" class="muted">free-text input — try it with the <b>Try</b> button.</div>`);
  }
  $('skIOCard').innerHTML = parts.join('') || '<span class="muted">—</span>';
}

function _renderSkillFiles(s){
  const card = $('skFilesCard');
  const hasFiles = s.files && s.files.length;
  if(!s.editable && !hasFiles){ card.style.display = 'none'; closeSkillFile(); return; }
  card.style.display = 'block';
  const chatBlocks = s.backend !== 'codex';
  $('skFilesHint').textContent = chatBlocks
    ? 'adding a file switches this skill to the codex backend (chat has no tool use)'
    : 'codex agent reads references / runs scripts';
  if(chatBlocks && !hasFiles){
    $('skFilesList').innerHTML = '<span class="muted" style="font-size:11px">chat-backend — adding a file flips it to codex.</span>';
  }else if(!hasFiles){
    $('skFilesList').innerHTML = '<span class="muted" style="font-size:11px">none yet — add a reference or script below.</span>';
  }else{
    $('skFilesList').innerHTML = s.files.map(f=>{
      const color = f.kind==='script' ? '#3b82f6' : '#6b7280';
      const touched = _skLastTouched && _skLastTouched.includes(f.path);
      const badge = touched ? ' <span style="color:var(--green);font-size:10px">✓ used</span>' : '';
      // the whole row is click-to-open (select + edit), like the skill catalog;
      // the explicit "open" button is kept as an affordance. Stop the button
      // click from double-firing the row handler.
      return `<div class="row" style="padding:4px 6px;border-radius:4px;align-items:center;gap:6px;cursor:pointer;border:1px solid transparent" onmouseover="this.style.background='var(--bg)'" onmouseout="this.style.background='';this.style.borderColor='transparent'" onclick="viewSkillFile('${esc(f.path)}')">
        <span style="font-size:9px;color:#fff;background:${color};padding:1px 5px;border-radius:8px">${esc(f.kind)}</span>
        <code style="flex:1;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.path)}</code>
        <span class="muted" style="font-size:10px">${f.size}B</span>${badge}
        <button class="sm" style="padding:2px 6px" onclick="event.stopPropagation();viewSkillFile('${esc(f.path)}')">open</button>
      </div>`;
    }).join('');
  }
  closeSkillFile();
}

// --- bundled-file editor (round 2, reused) --------------------------------
async function viewSkillFile(path){
  if(!_skCurrent) return;
  _skFilePath = path;
  $('skFilePath').textContent = path;
  $('skFileKindBadge').textContent = '';
  $('skFileStatus').textContent = 'loading…';
  $('skFileEditor').style.display = 'block';
  $('skFileBody').value = '';
  const isScript = path.startsWith('scripts/');
  $('skFileRunBtn').style.display = isScript ? 'inline-block' : 'none';
  const out = $('skFileOutput');
  out.style.display = 'none'; out.textContent = '';
  const [sub, fn] = path.split('/');
  if(sub && fn){ $('skFileKind').value = sub; $('skFileName').value = fn; }
  try{
    const r = await api('/skills/'+encodeURIComponent(_skCurrent)+'/files/'+path);
    $('skFileBody').value = r.content || '';
    $('skFileKindBadge').textContent = '('+r.kind+')';
    $('skFileStatus').textContent = '';
  }catch(e){
    $('skFileStatus').textContent = 'new file (unsaved)';
  }
}

async function runSkillFile(){
  if(!_skCurrent || !_skFilePath) return;
  if(!_skFilePath.startsWith('scripts/')){ toast('only scripts can run','err'); return; }
  // chat-backend skills have no tool use — the backend flips this skill to
  // codex when the script runs. Warn so the operator knows the AI path
  // changes from a single chat call to the codex agent harness.
  const wasChat = _skView && _skView.backend !== 'codex';
  if(wasChat) toast('running switches this skill to the codex backend','info');
  const out = $('skFileOutput');
  $('skFileStatus').textContent = 'running…';
  out.style.display = 'block';
  out.textContent = 'running…';
  try{
    const r = await api('/skills/'+encodeURIComponent(_skCurrent)+'/run-script', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({path: _skFilePath, content: $('skFileBody').value})});
    const dur = r.duration_ms != null ? r.duration_ms+'ms' : '';
    if(r.ok){
      const parts = [];
      parts.push('exit '+r.exit_code + (dur?('  ·  '+dur):''));
      if(r.stdout) parts.push('--- stdout ---\n'+r.stdout);
      if(r.stderr) parts.push('--- stderr ---\n'+r.stderr);
      if(!r.stdout && !r.stderr) parts.push('(no output)');
      out.textContent = parts.join('\n');
      out.style.color = r.exit_code === 0 ? '' : '#b45309';
      $('skFileStatus').textContent = r.exit_code === 0 ? 'ran ok'+(dur?('  '+dur):'') : 'exit '+r.exit_code;
      // a chat skill was flipped to codex on the backend — record it locally
      // so the next run doesn't re-toast (the catalog badge updates on next
      // open; we don't refresh the whole view here because that would close
      // this file editor and hide the output the operator just saw).
      if(wasChat && _skView) _skView.backend = 'codex';
    }else{
      out.textContent = (r.error||'run failed') + (r.stderr?('\n--- stderr ---\n'+r.stderr):'') + (r.stdout?('\n--- stdout ---\n'+r.stdout):'');
      out.style.color = '#dc2626';
      $('skFileStatus').textContent = r.error || 'failed';
    }
  }catch(e){
    out.textContent = 'run failed: '+e;
    out.style.color = '#dc2626';
    $('skFileStatus').textContent = '';
    toast('run failed: '+e, 'err');
  }
}

function addSkillFile(){
  if(!_skCurrent) return;
  const sub = $('skFileKind').value;
  const fn = $('skFileName').value.trim();
  if(!fn){ toast('enter a filename','err'); return; }
  if(sub==='scripts' && !fn.endsWith('.py')){ toast('script filenames must end in .py','err'); return; }
  if(_skView && _skView.backend !== 'codex'){
    // chat-backend skills have no tool use — the backend will flip this skill
    // to codex when the file is saved. Warn so the operator knows the AI path
    // changes from a single chat call to the codex agent harness.
    toast('saving will switch this skill to the codex backend','info');
  }
  viewSkillFile(sub+'/'+fn);
}

async function saveSkillFile(){
  if(!_skCurrent || !_skFilePath) return;
  $('skFileStatus').textContent = 'saving…';
  try{
    await api('/skills/'+encodeURIComponent(_skCurrent)+'/files/'+_skFilePath, {
      method:'PUT', headers:{'content-type':'application/json'},
      body: JSON.stringify({content: $('skFileBody').value})});
    $('skFileStatus').textContent = 'saved — takes effect immediately';
    toast('file saved','ok');
    await editSkill(_skCurrent);
  }catch(e){
    $('skFileStatus').textContent = '';
    toast('save file failed: '+e, 'err');
  }
}

async function deleteSkillFile(){
  if(!_skCurrent || !_skFilePath) return;
  if(!confirm('Delete "'+_skFilePath+'"?')) return;
  try{
    const r = await api('/skills/'+encodeURIComponent(_skCurrent)+'/files/'+_skFilePath,
                        {method:'DELETE'});
    if(!r.removed){ toast('file not found','err'); }
    else{ toast('file deleted','ok'); closeSkillFile(); await editSkill(_skCurrent); }
  }catch(e){
    toast('delete file failed: '+e, 'err');
  }
}

function closeSkillFile(){
  _skFilePath = null;
  const ed = $('skFileEditor');
  if(ed) ed.style.display = 'none';
  const st = $('skFileStatus');
  if(st) st.textContent = '';
}