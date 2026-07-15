/* ---------- init ---------- */
(async function(){
  await loadStats(); loadDashboard(); loadRail();
  // Pre-load the executor registry so the "Manage executors" card is populated
  // on page start (not only when the Code & DB tab is clicked). The card is
  // otherwise left on its static "loading…" default until loadCode() runs on
  // tab activation — which some stale-tab/cache states never trigger.
  // Guarded so it's a no-op if code.js hasn't defined loadExecutors yet.
  if(typeof loadExecutors === 'function') loadExecutors();
})();
