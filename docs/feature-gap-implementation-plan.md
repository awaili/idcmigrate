# Feature-gap implementation plan — extending the executor pattern

This plan closes the 11-feature audit gaps (see the deep audit, 2026-07-13) by
**extending the existing executor contract** rather than building heavy engines
in `idc-migrate`. The principle is the one the user stated:

> *The executor does the heavy lifting; idc-migrate manages it.*

"Manages" = owns the model, the storage row, the enrichment fold-in / gate, the
CLI command, the web surface, and the test. "Executor does" = the actual scan /
conversion / generation / metrics-pull, pushed back as a structured artifact.

## 0. The pattern to copy (already proven 5×)

Every existing executor action follows the same five-part shape. New features
copy it verbatim:

| Layer | Existing example (`db-scan`) | Where |
|---|---|---|
| **Request** (idc→executor) | `POST /v1/db-scan` | `executor_client.py:81 db_scan` |
| **Push** (executor→idc) | `PUT /api/db-profiles/{db_server_id}` | `backend/app.py:823` |
| **Model** | `DBConversionProfile` | `models.py:581` |
| **Store** | `db_profiles` table, `Store.upsert_db_profile` | `db.py:286, 1115` |
| **Enrichment / gate** | `codeintel.enrich_match_db` | `codeintel.py:464` |
| **Mock** | `_fake_db_profile`, `_run_db_scan` | `executor_mock/app.py:246, 286` |
| **Tests** | `test_db_conversion.py` | `tests/` |

So a feature is "done" when all seven rows exist. The audit found F1/F2/F3 have
real in-repo engines; F4/F8 have real management over a mock-only executor;
F5/F6/F9(runbook)/F10/F11 are absent or stub. This plan adds the missing rows.

## 1. Audit gap → work mapping

| # | Feature | Audit verdict | Where the work lives |
|---|---|---|---|
| 5 | IaC generation | 🔴 no generator, no guardrails | **new executor action `iac-emit`** + idc-migrate artifact store + guardrail gate |
| 6 | DB conversion | 🔴 grade-only, no converter | **extend `db-scan` → `db-convert`** + conversion-result store + compatibility report surface |
| 7 | Legacy OS rec | 🟡 detection real, rec stub | **new executor action `legacy-disposition`** + idc-migrate rec store + EOL→rec fold-in |
| 9 | Runbook/docs | 🟡 runbook only | **new executor actions `cutover-playbook` + `as-built`** + doc-artifact store + web/export |
| 10 | Post-mig optimization | 🔴 absent | **new executor action `postmig-optimize`** + post-mig metric ingest + recommendation store |
| 11 | Automated testing | 🔴 absent | **new executor actions `test-gen` + `test-run` + `test-compare`** + test-artifact store + cutover gate |
| 4/8 | Code/IP scan | 🟡 mock-only executor | **spec the real executor scanner** (executor side; idc-migrate management already done) |
| 1/2/3 | Discovery / 7R / waves | 🟢/🟡 in-repo | **small in-repo fixes** (netdep live path, 7R confidence formula, downtime windows) |

## 2. Phasing

- **Phase 1 — extend existing flows (lowest risk).** F6 db-convert (builds on
  `db-scan`), F7 legacy-disposition (builds on EOL detection + runtime-containerize),
  F9 cutover-playbook + as-built (builds on `assess_wave` + `MigrationJob`).
  Plus the small in-repo fixes (F1 netdep, F2 confidence, F3 downtime).
- **Phase 2 — new artifact pipelines.** F5 IaC emit + guardrails, F10 post-mig
  optimization (needs a post-mig metric ingest path).
- **Phase 3 — the big one.** F11 automated testing (test-gen / run / compare +
  baseline-capture gate on cutover).

Each phase is independently shippable; each feature within a phase is
independent. The mock implementation lands first in every case so the full loop
runs locally before a real executor exists.

---

## Phase 1

### F6 — Database conversion: extend `db-scan` → `db-convert`

**Gap.** `DBConversionProfile` carries only a difficulty grade + man-days +
blockers. No schema/SQL is converted; nothing emits a compatibility report with
per-object items; no PostgreSQL target.

**Executor contract addition.** Add a `mode` to the existing `db-scan` action
(keeps the grade path as `mode=assess`, the default), and a new
`mode=convert` that also returns the converted artifacts.

- Request: `POST /v1/db-scan` gains `mode: "assess"|"convert"` (default
  `assess`, back-compat). In `convert` mode the executor also emits converted
  DDL + a per-object compatibility report.
- Push: `PUT /api/db-profiles/{db_server_id}` body gains an optional
  `conversion` block (omitted in `assess` mode → old clients unaffected):
  ```json
  "conversion": {
    "target_engine": "postgresql",
    "ddl": ["CREATE TABLE orders (...)", "..."],          // converted DDL
    "objects": [
      {"name": "PKG_ORDERS", "kind": "package",
       "status": "manual_review",                         // auto_converted|manual_review|blocked
       "converted": "", "issue": "DBMS_LOCK has no PG equivalent",
       "effort_days": 3.0}
    ],
    "auto_convert_pct": 0.62,                             // = auto_converted/total
    "report_md": "...markdown compatibility report..."
  }
  ```
- `target_engine` becomes a free choice (`tdsql`, `cdb_mysql`, `postgresql`,
  ...); the mock adds a PG path so the missing target is closed.

**idc-migrate management layer.**
- Model: extend `DBConversionProfile` with `conversion: Optional[DBConversion]`
  (`models.py`, new dataclass `DBConversion` with `ddl`, `objects`,
  `auto_convert_pct`, `report_md`).
- Store: `db_profiles.conversion` column (JSON); `Store.upsert_db_profile` +
  `get_db_profile` carry it. New list endpoints already exist.
- Enrichment: `codeintel.enrich_match_db` unchanged (still grade-driven). Add
  `codeintel.db_conversion_summary(profile)` → markdown for the web detail
  view. Gate: a `db-kind` wave's `cutover` gate already checks
  `reverse_replication`; add `conversion.objects` blocked-count > 0 → block
  `finalized` until operator acks (reuse the Question channel).
- CLI: `idc db-profile <db_server_id>` prints the compatibility report;
  `idc db-convert <db_server_id> --target postgresql` triggers `mode=convert`.
- Web: `GET /api/db-profiles/{id}` returns `conversion`; a "Conversion report"
  tab renders `report_md`.
- Mock: `_run_db_scan` branches on `mode`; `_fake_db_conversion(...)` returns
  canned converted DDL + 3 objects (auto/manual/blocked) per source engine,
  incl. an Oracle→PG path.
- Tests: `test_db_conversion.py` adds `test_convert_mode_returns_ddl_and_report`,
  `test_assess_mode_omits_conversion` (back-compat), `test_blocked_objects_block_finalize`.

### F7 — Legacy / unsupported OS recommendation: `legacy-disposition`

**Gap.** `apply_os_eol` appends one fixed string ("replatform to a supported
base image") to every EOL host. No containerize/replatform/rewrite decision.

**Executor contract addition.** New action that analyzes a single host's
workload context and returns a disposition recommendation.

- Request: `POST /v1/legacy-disposition`
  ```json
  {"server_id": "...", "app_id": "legacy-billing",
   "os": "centos 6.5", "os_eol_bucket": "expired", "runtime": "jdk7",
   "role": "app", "criticality": "high",
   "has_source_repo": false,
   "runtime_inventory": {"process": "...", "port": 8080, "software": ["openjdk-7"]},
   "code_profile_summary": "..."}
  ```
- Push: `PUT /api/legacy-dispositions/{server_id}`
  ```json
  {"server_id": "...", "disposition": "containerize",  // containerize|replatform|rewrite|retain
   "rationale": "stateless web tier, no source repo, JDK7 → containerize on supported base",
   "confidence": 0.7, "target_base_image": "tencentos-server-3.1:jdk17",
   "effort_days": 5.0, "prereqs": ["capture runtime inventory", "validate port 8080"],
   "scan_id": "cjob-...", "scanned_at": "..."}
  ```

**idc-migrate management layer.**
- Model: `LegacyDisposition` dataclass (`models.py`).
- Store: `legacy_dispositions` table keyed by `server_id` (stable hostname, like
  db-profiles); `Store.upsert_legacy_disposition` / `list_legacy_dispositions` /
  `get_legacy_disposition`.
- Enrichment: `eol.apply_os_eol` keeps its confidence dip, but the appended
  `alternative` now reads from the stored `LegacyDisposition.disposition` +
  `rationale` if present (fallback to the current fixed string when no
  disposition exists — back-compat). `non_migrating_servers` /
  `disposition_waves` already pull retain/retire; add `rewrite`-disposition apps
  into a trailing wave.
- Trigger: auto-fire `legacy-disposition` during `rebuild` for any host whose
  `os_eol_bucket ∈ {expired, expiring}` and has no stored disposition.
- CLI: `idc legacy-disposition <server_id>` (force re-run).
- Web: a "Legacy OS" column on the server table → detail modal with the
  recommendation + prereqs + a "containerize" button that chains into the
  existing `runtime-containerize` action.
- Mock: `_fake_legacy_disposition(...)` returns containerize for source-less
  stateless web, replatform for stateful app, rewrite for tightly-coupled
  legacy — keyed on `role`/`has_source_repo`/`runtime_inventory`.
- Tests: `test_legacy_disposition.py` — fold-in override, retain/rewrite
  routing, auto-trigger on EOL, back-compat fallback.

### F9 — Cutover playbook + as-built docs

**Gap.** Only a per-wave runbook JSON (`assess_wave`). No cutover playbook, no
as-built doc; execution data (`MigrationJob.stage_history`, gate results,
`ChangeJob.patch_ref`) is persisted but never rendered.

**Executor contract addition.** Two new doc-generation actions. The executor
generates grounded markdown from data idc-migrate sends (keeps idc-migrate
LLM-free for these).

- `POST /v1/cutover-playbook`
  ```json
  {"wave_id": "w-3", "app_ids": ["orders-svc","user-svc"],
   "members": [{"server_id":"...","role":"db","target":"CDB","reverse_replication":true,
                "deps":["inventory-svc"],"ports":[3306,1521]}],
   "downtime_window": {"start":"2026-07-20T02:00Z","end":"2026-07-20T04:00Z"},
   "risk_basis": {"score":7,"level":"high","factors":[...]}}
  ```
  Push: `PUT /api/docs/cutover/{wave_id}` → `{"wave_id","doc_md","scan_id","scanned_at"}`.
  Output: ordered per-server cutover steps (pre-checks → replication cut →
  flip DNS → smoke → rollback), with the reverse-replication teardown for DB hosts.

- `POST /v1/as-built`
  ```json
  {"wave_id":"w-3","job_ids":["mjob-1","mjob-2"],
   "stage_history": [...], "gate_results": [...], "change_jobs":[...],
   "targets":[...]}
  ```
  Push: `PUT /api/docs/as-built/{wave_id}` → same shape. Output: as-built doc
  (what moved, to where, final spec, gate outcomes, patch refs, deviations).

**idc-migrate management layer.**
- Model: `DocArtifact` dataclass (`doc_type ∈ {cutover, as_built, runbook}`,
  `scope_id`, `doc_md`, `scan_id`, `scanned_at`). The existing `assess_wave`
  runbook gets persisted as a `DocArtifact(doc_type=runbook, scope_id=wave_id)`
  too — closes the "no doc artifact" gap.
- Store: `doc_artifacts` table keyed by `(doc_type, scope_id)`; upsert + list +
  get. `Store.save_doc_artifact` / `list_doc_artifacts`.
- Trigger: `cutover-playbook` auto-fires when a wave enters `ready` /
  pre-cutover; `as-built` auto-fires when a wave reaches `finalized`.
- CLI: `idc doc cutover <wave_id>`, `idc doc as-built <wave_id>`,
  `idc doc runbook <wave_id>`, `idc doc list`.
- Web: a "Docs" tab per wave; render `doc_md`; export button (download `.md`).
- Mock: `_fake_cutover_playbook(...)` / `_fake_as_built(...)` build templated
  markdown from the inputs (real executor would use its LLM).
- Tests: `test_docs.py` — generation + persist + retrieve + back-compat
  (`assess_wave` still returns the JSON inline).

### Phase 1 in-repo fixes (F1 / F2 / F3)

These are NOT executor work — they fix existing in-repo engines.

- **F1 netdep live path.** `netdep.py:91-104` `_from_zabbix` returns `[]`;
  the prometheus/zabbix paths pass empty metric strings (`netdep.py:121`) so
  always fall back to fixture. Fix: either (a) wire a real custom-exporter
  metric name in config (`IDC_NETDEP_PROM_METRIC`), or (b) document the
  custom-exporter requirement and mark the live paths disabled-by-default
  honestly (remove the dead `_from_zabbix` stub or finish it). Prefer (a):
  add `IDC_NETDEP_PROM_METRIC` + a real query in `PrometheusAdapter` for
  per-connection dst ip:port from a documented exporter.
- **F2 7R confidence formula.** `LLMClient.seven_r_strategy` trusts the LLM's
  `confidence` (`client.py:729`). Add `codeintel.seven_r_confidence(signals)`
  — a deterministic formula fusing `cloud_readiness`, blocker count,
  `refactor_effort`, and `assessment_confidence_of` (data-coverage) — and use
  it to **clamp/cap** the LLM-emitted confidence (LLM can lower it, not raise
  it above the formula). Keeps the LLM reasoning, adds a real floor/ceiling.
- **F3 downtime windows.** Add `downtime_window` to `Wave`/`WaveSpec`
  (`{start, end, tz}`) + a `downtime_windows` filter on `WaveSpec` so the LLM
  policy can batch by maintenance slot. `plan_waves` default: if a wave's
  members declare a window, surface it in rationale; no scheduler yet (the LLM
  policy path handles it). Add `business_criticality` as a first-class
  ordering key in `plan_waves` so high-crit apps aren't buried in late waves.

---

## Phase 2

### F5 — IaC generation: `iac-emit` + guardrail gate

**Gap.** No Terraform/ARM/Bicep anywhere; `LZ_BLUEPRINTS` is input to an LLM
prompt; "Well-Architected guardrails" is hand-wave (`policy_as_code` strings,
`check_lz_gate` checks an operator flag).

**Executor contract addition.** The executor emits structured IaC + runs the
guardrail checks; idc-migrate stores + gates.

- Request: `POST /v1/iac-emit`
  ```json
  {"scope": "landing_zone", "archetype": "corp",
   "blueprint": {...},                          // from LZ_BLUEPRINTS
   "target": {"cloud":"tencent","region":"shanghai"}}
  ```
  or `{"scope":"workload","server_id":"...","match":{...}}`.
- Push: `PUT /api/iac-artifacts/{scope_id}`
  ```json
  {"scope_id":"lz:corp", "scope":"landing_zone",
   "modules": [{"path":"main.tf","content":"resource \"tencentcloud_vpc\" ..."},
                {"path":"variables.tf","content":"..."}],
   "guardrails": [
     {"pillar":"security","rule":"no_public_clb_in_corp",
      "status":"pass", "finding":"", "severity":"high"},
     {"pillar":"cost","rule":"required_tags",
      "status":"fail", "finding":"vpc-corp missing cost_center tag","severity":"medium"}],
   "guardrail_pass": false,
   "plan_summary": "Plan: 12 to add, 0 to change, 0 to destroy.",
   "scan_id":"cjob-..."}
  ```

**idc-migrate management layer.**
- Model: `IaCArtifact` (`modules`, `guardrails`, `guardrail_pass`,
  `plan_summary`). `GuardrailCheck` dataclass (`pillar`, `rule`, `status`,
  `finding`, `severity`).
- Store: `iac_artifacts` table keyed by `scope_id` (`lz:<arch>` /
  `wl:<server_id>`).
- Guardrail gate (the real "checked against Well-Architected guardrails"):
  `codeintel.check_iac_guardrails(artifact)` aggregates `guardrails` →
  `ok = no fail at severity≥medium`. Wire into `check_lz_gate` (`lz.py:203`):
  a wave targeting an archetype is blocked from `launch` until that
  archetype's latest `IaCArtifact.guardrail_pass` is true (replaces the
  operator-flag-only check). The `LZ_BLUEPRINTS.policy_as_code` strings become
  the **rule ids** the executor evaluates against.
- CLI: `idc iac emit --landing-zone corp`, `idc iac emit --server <id>`,
  `idc iac show <scope_id>`, `idc iac guardrails <scope_id>`.
- Web: "IaC" tab on archetype + server detail; render modules (with download
  zip); guardrail pass/fail panel; `guardrail_pass=false` blocks the Launch
  button with a clear reason.
- Mock: `_fake_iac_artifact(...)` emits 2-3 `resource "tencentcloud_*"` blocks
  from the blueprint + 2 guardrail checks (one pass, one fail) so the gate is
  exercisable.
- Tests: `test_iac.py` — emit + persist + retrieve; guardrail gate blocks
  launch on fail, allows on pass; `check_lz_gate` integration.

### F10 — Post-migration optimization: `postmig-optimize`

**Gap.** "right_size" is pre-migration only; no anomaly detection, no
post-mig right-sizing, no reserved-capacity recommendation, no perf tuning.

**Executor contract addition.** The executor pulls post-mig cloud metrics
(its own Cloud Monitor / Prometheus access) + returns recommendations.

- Request: `POST /v1/postmig-optimize`
  ```json
  {"server_id":"...", "app_id":"orders-svc",
   "target":{"product":"CVM","spec":"SA2.LARGE8","region":"shanghai"},
   "window_days":30,
   "metrics": {...}}   // optional — idc-migrate may forward Prometheus data;
                       //                  else executor pulls from Cloud Monitor
  ```
- Push: `PUT /api/postmig-recs/{server_id}`
  ```json
  {"server_id":"...", "recommendations": [
     {"kind":"right_size","from":"SA2.LARGE8","to":"SA2.MEDIUM4",
      "reason":"p95 cpu 8%, mem 22% over 30d","monthly_saving":120.0,"confidence":0.8},
     {"kind":"reserved","term":"1yr","reason":"steady-state > 80% uptime",
      "monthly_saving":60.0,"confidence":0.7},
     {"kind":"anomaly","metric":"cpu","detail":"spike 2026-07-10T03:00Z","severity":"medium"},
     {"kind":"perf","detail":"DB connection pool saturated at 95%; raise pool size"}
   ], "scan_id":"cjob-...","scanned_at":"..."}
  ```

**idc-migrate management layer.**
- New ingest: `PostMigMetrics` — either forward Prometheus via the existing
  adapter or add a `cloud_monitor` ingest (`idc/core/ingest/cloud_monitor.py`)
  reading Tencent Cloud Monitor metrics for migrated resources. Start with
  forwarding (no new adapter) to minimize scope.
- Model: `PostMigRecommendation` (`kind`, `from`/`to`, `reason`,
  `monthly_saving`, `confidence`, `severity`).
- Store: `postmig_recs` table keyed by `server_id`; upsert + list + get.
- Trigger: a scheduled sweep (`postmig-optimize.timer`-style) over
  `finalized` hosts ≥ N days post-cutover; or operator-triggered per host.
- Cost integration: `cost.what_if` already computes cloud yearly; feed the
  `right_size`/`reserved` recs back into `cost` to show realized savings.
- CLI: `idc postmig scan [--server <id>|--all]`, `idc postmig recs <server_id>`.
- Web: "Post-migration" tab on finalized hosts; recommendation cards with
  savings + confidence + "apply" (chains to `iac-emit` for a resize).
- Mock: `_fake_postmig_recs(...)` returns one of each kind with canned savings.
- Tests: `test_postmig.py` — ingest forward, rec persist/retrieve, cost
  rollup of savings, trigger only on finalized hosts.

---

## Phase 3

### F11 — Automated testing: `test-gen` + `test-run` + `test-compare`

**Gap.** Nothing. No test-case generation, no functional/integration/
regression runner, no pre-vs-post comparison. The nearest primitive is
`run_validation_gates` (liveness probes, with `process_up` /
`volume_consistency` as manual `pending` stubs).

**Executor contract addition.** Three actions; the comparison is the load-
bearing piece.

- `POST /v1/test-gen` — generate test cases from the app's `CodeProfile` +
  workload context.
  Push: `PUT /api/test-artifacts/{app_id}` with `test_cases[]`
  (`name`, `kind ∈ functional|integration|regression`, `endpoint`, `method`,
  `request`, `expected`, `setup`).
- `POST /v1/test-run` — run a test set against a target (pre-mig on-prem, or
  post-mig cloud).
  Push: `PUT /api/test-results/{run_id}` with `results[]` (`case`, `status`,
  `duration_ms`, `response_summary`, `error`).
- `POST /v1/test-compare` — diff a pre-run vs a post-run.
  Push: `PUT /api/test-diffs/{app_id}` with `diff[]` (`case`, `pre_status`,
  `post_status`, `verdict ∈ pass|regression|flaky|new_failure`, `detail`).

**idc-migrate management layer.**
- Model: `TestCase`, `TestRun`, `TestResult`, `TestDiff` (`models.py`).
- Store: `test_cases` (per app), `test_runs` + `test_results` (per run),
  `test_diffs` (per app, latest) tables.
- Baseline capture: when a wave enters `ready`, auto-run `test-gen` + a pre-mig
  `test-run` against the on-prem endpoints → store as the baseline `TestRun`
  tagged `phase=pre`.
- Post-mig run: when a wave reaches `cutover` done, run `test-run` against the
  new cloud endpoints → `phase=post`; then `test-compare` → `TestDiff`.
- Cutover gate: a new validation gate `test_regression` (replaces the manual
  `process_up` stub): `finalized` blocked while any `TestDiff.verdict ∈
  {regression, new_failure}` unresolved. Operator can ack-skip per case (the
  Question channel).
- CLI: `idc test gen <app_id>`, `idc test run <app_id> --phase pre|post`,
  `idc test compare <app_id>`, `idc test diffs [--pending]`.
- Web: "Testing" tab per app; pre/post diff table; regression cards; gate
  status on the wave.
- Mock: `_fake_test_cases(...)` from the `CodeProfile` (e.g. for a Spring Boot
  app, generate a health-check + one endpoint case); `_fake_test_run` returns
  pass/fail; `_fake_test_compare` returns one regression to exercise the gate.
- Tests: `test_automated_testing.py` — gen+persist, pre baseline capture on
  `ready`, post run on cutover, compare → diff, gate blocks on regression,
  ack-skip unblocks, `process_up` stub replaced.

---

## 3. Cross-cutting conventions (copy from existing work)

- **Stable keys.** DB profiles key by hostname (not `Server.id`); legacy
  dispositions, postmig recs, test artifacts key by `app_id` or stable
  `server_id` — never the rebuild-changing `Server.id`. (Existing convention,
  `docs/agent-executor.md` §1.5.)
- **Push endpoints** live behind the existing bearer gate (`/api/*`); no new
  ports/certs.
- **Mock-first.** Every new action gets a mock in `executor_mock/app.py`
  before a real executor exists — the full loop runs locally. The mock is the
  executable spec.
- **Graceful degradation.** When `IDC_EXECUTOR_URL` empty or unreachable, the
  feature no-ops (never crashes `rebuild`/`plan`); enrichment falls back to
  existing behavior. Mirror `db-scan`'s pattern.
- **Idempotent upserts.** Every push is upsert-by-key, no duplicate rows.
- **The Question channel** (`POST /api/apps/{app_id}/questions`) is reused for
  any "don't guess" moment — DB blocked objects, guardrail overrides, test
  ack-skip. No new blocking primitive.
- **English-only** for all new identifiers/comments/docstrings/commits
  (project rule, memory `code-in-english`).
- **Tests share the `idc_migrate_test` DB**; don't run pytest in parallel
  (memory `regression-tests-share-db`).

## 4. Effort & sequencing notes

- Phase 1 features are each ~1 mock + ~1 model + ~1 store table + ~1 CLI +
  ~1 web tab + ~1 test file — same shape as `db-scan` was. F9 and F7 are the
  most net-new; F6 is mostly extending an existing model.
- Phase 2 F5 is the largest after F11 — the guardrail gate replacing the
  operator flag is the load-bearing change. F10 needs a decision on the
  metrics ingest path (forward Prometheus vs. new Cloud Monitor adapter).
- Phase 3 F11 is the biggest; land `test-gen`+`test-run` first, then
  `test-compare` + the cutover gate (the gate is the value).
- The in-repo Phase-1 fixes (F1/F2/F3) are independent of the executor work
  and can ship first — they improve already-working engines.

## 5. Open decisions (default chosen, flag if wrong)

1. **F10 metrics source** — default: forward Prometheus via the existing
  adapter (no new ingest). Alt: new `cloud_monitor` ingest. Revisit when a
  real executor's metric access is known.
2. **F9 doc generation** — default: executor (keeps idc-migrate LLM-free).
  Alt: reuse idc-migrate's own `LLMClient` like `assess_wave` does. Default
  chosen to keep the contract uniform; `assess_wave`'s inline JSON stays for
  back-compat.
3. **F5 target clouds** — default: Tencent only (matches `LZ_BLUEPRINTS` +
  `claude_runner` preamble). The contract is cloud-agnostic by design; a
  multi-cloud executor can emit AWS/ARM HCL with no idc-migrate change.
4. **F11 pre-mig endpoint** — default: tests hit the on-prem endpoint the app
  already exposes (from `runtime_inventory`/`network_endpoints`). Assumes
  reachability from the executor; if not, executor runs from inside the DC.