/* ---------- pending-questions queue (executor ↔ operator, channel ④) ----------
 * The executor raises a question here when it can't resolve a change (resolve
 * returned unknown, or a change's old/new was empty). The operator answers;
 * the executor polls GET /api/questions/{id} and continues. This panel is the
 * operator-facing half of that loop. */
let qTimer = null;

/* poll the pending-questions queue while the Code tab is around; idempotent. */
function startQuestionPolling(){
  if(qTimer) return;
  qTimer = setInterval(loadQuestions, 10000);
}

async function loadQuestions(){
  let qs; try{ qs = await api('/questions?status=pending'); }catch(e){ $('qList').innerHTML='<span class="ev-err">Error: '+esc(e)+'</span>'; return; }
  const el = $('qList');
  if(!qs.length){ el.innerHTML = '<span class="muted">No pending questions. The executor raises one here when it can\'t resolve a change (e.g. an unknown hardcoded IP).</span>'; return; }
  el.innerHTML = qs.map(q=>{
    const ctx = q.context || {};
    const loc = ctx.file ? `${esc(ctx.file)}${ctx.line?':'+ctx.line:''}` : '(location unknown)';
    const oldNew = [];
    if(ctx.old) oldNew.push(`old: <code>${esc(ctx.old)}</code>`);
    if(ctx.new) oldNew.push(`new: <code>${esc(ctx.new)}</code>`);
    // escape for a JS single-quoted string literal inside an onclick attribute
    // attr() = JS-string + HTML-attribute safe (escapes \ ' then & < > ") —
    // correct for inlining an option into onclick="answerQuestion('...','...')".
    const opts = (q.options||[]).map(o=>`<button class="sm" onclick="answerQuestion('${q.id}','${attr(o)}')">${esc(o)}</button>`).join(' ');
    return `<div class="card qcard">
      <div class="row" style="justify-content:space-between">
        <b>${esc(q.app_id)}</b>
        <span class="tag">${esc(q.kind)}</span>
      </div>
      <div style="margin:6px 0">${esc(q.prompt)}</div>
      <div class="muted" style="font-size:11px">📍 ${loc}${ctx.category?' · '+esc(ctx.category):''}${oldNew.length?' · '+oldNew.join(' '):''}</div>
      <div class="row" style="margin-top:8px;gap:6px">
        <input id="qa-${q.id}" placeholder="answer…" style="flex:1;min-width:160px"
               onkeydown="if(event.key==='Enter')answerQuestion('${q.id}',this.value)"/>
        <button class="primary sm" onclick="answerQuestion('${q.id}',document.getElementById('qa-${q.id}').value)">Answer</button>
        <button class="sm" onclick="skipQuestion('${q.id}')">Skip</button>
      </div>
      ${opts?`<div class="row" style="margin-top:6px;gap:6px"><span class="muted">suggested:</span>${opts}</div>`:''}
    </div>`;
  }).join('');
}

async function answerQuestion(qid, ans){
  ans = (ans||'').trim();
  if(!ans) return;
  try{ await api(`/questions/${qid}/answer`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({answer:ans,answered_by:'ui'})}); }
  catch(e){ toast('Answer failed: '+e,'err'); return; }
  loadQuestions();
}

async function skipQuestion(qid){
  try{ await api(`/questions/${qid}/skip`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({reason:'skipped by operator'})}); }
  catch(e){ toast('Skip failed: '+e,'err'); return; }
  loadQuestions();
}