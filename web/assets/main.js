/* ---------- init ---------- */
(async function(){
  await loadStats(); loadDashboard(); loadRail();
  // Pre-load the executor connection config so the "Manage executor" card is
  // populated on page start (not only when the Code & DB tab is clicked). The
  // card is otherwise left on its static "loading…" default until loadCode()
  // runs on tab activation — which some stale-tab/cache states never trigger.
  // Guarded so it's a no-op if code.js hasn't defined loadExecConfig yet.
  if(typeof loadExecConfig === 'function') loadExecConfig();
})();
