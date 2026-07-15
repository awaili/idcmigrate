/* ---------- F11 — automated testing (generate / run / compare) ---------- */
async function loadTesting(){
  loadTestDiffs();
  loadTestRuns();
  loadTestCases();
}

async function loadTestCases(){
  const tb = $('tcTbl').querySelector('tbody'); tb.innerHTML='';
  try{
    const cases = await api('/test-cases');
    cases.forEach(c=> tb.insertAdjacentHTML('beforeend', `<tr>
      <td data-label="app_id">${esc(c.app_id||'-')}</td>
      <td data-label="name">${esc(c.name||'-')}</td>
      <td data-label="kind">${esc(c.kind||'-')}</td>
      <td data-label="endpoint">${esc(c.endpoint||'-')}</td>
      <td data-label="method">${esc(c.method||'-')}</td></tr>`));
    if(!cases.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="5" class="muted">no test cases yet — generate some for an app.</td></tr>`);
  }catch(e){ tb.innerHTML = `<tr><td colspan="5" class="ev-err">${esc(e)}</td></tr>`; }
}

async function loadTestRuns(){
  const tb = $('trTbl').querySelector('tbody'); tb.innerHTML='';
  try{
    const runs = await api('/test-runs');
    runs.forEach(r=>{
      const pcol = r.phase==='pre' ? 'var(--amber)' : r.phase==='post' ? 'var(--green)' : 'var(--fg)';
      tb.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="run_id">${esc(r.id)}</td>
        <td data-label="app_id">${esc(r.app_id||'-')}</td>
        <td data-label="phase"><span style="color:${pcol};font-weight:600">${esc(r.phase||'-')}</span></td>
        <td data-label="pass">${r.passed||0}</td>
        <td data-label="fail" style="color:${r.failed?'var(--red)':'var(--fg)'}">${r.failed||0}</td>
        <td data-label="run_at">${esc(r.run_at||'-')}</td></tr>`);
    });
    if(!runs.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="6" class="muted">no test runs yet.</td></tr>`);
  }catch(e){ tb.innerHTML = `<tr><td colspan="6" class="ev-err">${esc(e)}</td></tr>`; }
}

async function loadTestDiffs(){
  const tb = $('tdTbl').querySelector('tbody'); tb.innerHTML='';
  try{
    const diffs = await api('/test-diffs');
    diffs.forEach(d=>{
      const col = d.regressions ? 'var(--red)' : 'var(--green)';
      tb.insertAdjacentHTML('beforeend', `<tr>
        <td data-label="app_id">${esc(d.app_id||'-')}</td>
        <td data-label="regressions"><span style="color:${col};font-weight:600">${d.regressions||0}</span></td>
        <td data-label="summary" class="conf" title="${esc(d.summary||'')}">${esc((d.summary||'-').slice(0,60))}</td>
        <td data-label="updated">${esc(d.updated_at||d.scanned_at||'-')}</td>
        <td data-label="detail"><button class="sm" onclick="showTestDiff('${attr(d.app_id)}')">detail</button></td></tr>`);
    });
    if(!diffs.length) tb.insertAdjacentHTML('beforeend', `<tr><td colspan="5" class="muted">no test diffs yet — run pre + post, then compare.</td></tr>`);
  }catch(e){ tb.innerHTML = `<tr><td colspan="5" class="ev-err">${esc(e)}</td></tr>`; }
}

async function showTestDiff(appId){
  const el = $('tdDetail'); el.classList.remove('hidden'); el.innerHTML='<span class="spinner"></span>';
  try{
    const d = await api('/test-diffs/'+encodeURIComponent(appId));
    const vcol = {pass:'var(--green)', regression:'var(--red)', new_failure:'var(--red)', flaky:'var(--amber)'};
    const rows = (d.diff||[]).map(it=>`<tr>
      <td>${esc(it.case||'-')}</td>
      <td>${esc(it.pre_status||'-')}</td>
      <td>${esc(it.post_status||'-')}</td>
      <td style="color:${vcol[it.verdict]||'var(--fg)'};font-weight:600">${esc(it.verdict||'-')}</td>
      <td>${esc(it.detail||'—')}</td></tr>`).join('');
    el.innerHTML = `<div><b>${esc(d.app_id)}</b>  ·  ${d.regressions?'<span style="color:var(--red)">'+d.regressions+' regression(s) — finalize blocked</span>':'<span style="color:var(--green)">clean</span>'}</div>
      <div class="xscroll" style="margin-top:6px"><table class="tbl mcard"><thead><tr><th>case</th><th>pre</th><th>post</th><th>verdict</th><th>detail</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }catch(e){ el.innerHTML='<span class="ev-err">'+esc(e)+'</span>'; }
}

async function doTestGen(){
  const app_id = $('tcApp').value.trim();
  if(!app_id){ toast('enter an app_id','warn'); return; }
  try{
    await api('/test-gen', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({app_id, context:{}})});
    toast('test-gen requested — cases arrive via callback.', 'ok');
    setTimeout(loadTesting, 1500);
  }catch(e){ toast('test-gen failed: '+e, 'err'); }
}

async function doTestRun(phase){
  const app_id = $('tcApp').value.trim();
  const target = $('tcTarget').value.trim();
  if(!app_id){ toast('enter an app_id','warn'); return; }
  if(!target){ toast('enter a target endpoint base','warn'); return; }
  try{
    await api('/test-run', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({app_id, phase, target, context:{}})});
    toast(`test-run (${phase}) requested — results arrive via callback.`, 'ok');
    setTimeout(loadTesting, 1500);
  }catch(e){ toast('test-run failed: '+e, 'err'); }
}

async function doTestCompare(){
  const app_id = $('tcApp').value.trim();
  if(!app_id){ toast('enter an app_id','warn'); return; }
  try{
    // need the latest pre + post runs for the app (Array.find returns the
    // OLDEST match — after a re-baseline that would compare against a stale
    // pre run and feed the test_regression cutover gate a wrong verdict).
    const runs = await api('/test-runs?app_id='+encodeURIComponent(app_id));
    const latest = phase => runs.filter(r=>r.phase===phase)
        .sort((a,b)=>String(a.run_at||'').localeCompare(String(b.run_at||'')))
        .pop();
    const pre = latest('pre');
    const post = latest('post');
    if(!pre || !post){ toast('need both a pre and a post run first','warn'); return; }
    await api('/test-compare', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({app_id, pre_run_id:pre.id, post_run_id:post.id})});
    toast('test-compare requested — diff arrives via callback.', 'ok');
    setTimeout(loadTesting, 1500);
  }catch(e){ toast('test-compare failed: '+e, 'err'); }
}