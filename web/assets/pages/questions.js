/* ---------- pending-questions queue (executor ↔ operator, channel ④) ----------
 * The executor raises a question when it can't resolve a change (resolve returned
 * unknown, or a change's old/new was empty). The operator answers; the executor
 * polls GET /api/questions/{id} and continues.
 *
 * On the Scan & Migrate page this queue is scoped to the loaded workload and
 * rendered by loadWorkloadQuestions() in pages/code.js (which also runs a
 * per-workload poller). answerQuestion / skipQuestion live here so the executor
 * contract side stays in one file; both refresh the workload's queue. */
async function answerQuestion(qid, ans){
  ans = (ans||'').trim();
  if(!ans) return;
  try{ await api(`/questions/${qid}/answer`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({answer:ans,answered_by:'ui'})}); }
  catch(e){ toast('Answer failed: '+e,'err'); return; }
  if(typeof loadWorkloadQuestions === 'function') loadWorkloadQuestions();
}

async function skipQuestion(qid){
  try{ await api(`/questions/${qid}/skip`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({reason:'skipped by operator'})}); }
  catch(e){ toast('Skip failed: '+e,'err'); return; }
  if(typeof loadWorkloadQuestions === 'function') loadWorkloadQuestions();
}