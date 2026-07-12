# Fusion Implementation Plan

> Companion to `docs/competitor-fusion-design.md`. This is the *how*:
> concrete, symbol-grounded steps to fuse F1–F11 into idc-migrate, in 3
> phases. Every item names the real files/symbols it touches (verified
> against the codebase 2026-07). Effort: **S** ≤1 day, **M** 2–4 days,
> **L** 1–2 weeks.
>
> **Two open decisions, defaulted here so the plan is actionable:**
> - **F2 pricing source** → default = **Tencent Cloud public pricing API + an
>   override hook** (same `old→new` map shape as the executor overrides), so
>   customer contract pricing can be plugged in later without rework.
> - **F6 execution depth** → default = **Phase 2 ships state-tracking +
>   manual/external "mark complete" + declarative validation gates driven by
>   the agent + Prometheus**. Real Tencent **SMS/DTS driver** integration is a
>   follow-on increment (Phase 2.5), gated on SMS API access. This keeps
>   Phase 2 unblocked.

---

## Phase 0 — Foundations (unblock everything)

Small shared changes that two or more F-items depend on. Do these first,
once.

### P0a — Cost + readiness fields on the models — S
`idc/core/models.py`:
- Add `CostEstimate` dataclass: `server_id, target_product, region, monthly_usd, yearly_usd, basis` (measured/estimated), `pricing_source` (public/contract), `computed_at`. TEXT-serialized like the rest.
- Add `assessment_confidence: Optional[float]` and `sizing_basis: str` ("measured"/"estimated") to `Server`.
- Add `cost: Optional[CostEstimate]` to `Match` (or a side table — see P0b).
- Add `MigrationJob` dataclass (for F6, but the model lands here): `id` (`mjob-…`), `server_id, app_id, kind` (host/db/code), `executor_ref` (SMS job id / DTS job id / ChangeJob id), `status` (`planned/replicating/tested/ready_for_cutover/cut_over/finalized/rolled_back`), `stage_history:List[Dict]`, `validation_gates:List[Dict]`, `created_at, updated_at`.
- Constants: `MIGRATION_JOB_STATUSES`, `MIGRATION_JOB_KINDS`, and the state-machine adjacency dict `MIGRATION_JOB_TRANSITIONS` (defines revert edges).
- New finding categories for F5 land here too (defer the `DBConversionProfile` to F5, but add `plsql_compat, db_feature_gap, db_size_complexity` to `FINDING_CATEGORIES` now so the executor contract is forward-compatible).

### P0b — Storage schema + migrations — S/M
`idc/core/db.py`:
- Extend `SCHEMA` (L63) with tables: `cost_estimates(server_id PK, ...)` (one-row-per-server, upserted on rebuild), `migration_jobs(id PK, server_id, status, ...)` (indexed by `server_id` and `status`), `business_case_snapshots(id PK, created_at, payload TEXT)` (immutable snapshots of a business-case run).
- `Store` methods: `upsert_cost(server_id, est)`, `get_cost(server_id)`, `list_costs(filter)`, `upsert_migration_job`, `get_migration_job`, `list_migration_jobs(filter)`, `save_business_case(snap)`, `list_business_cases`.
- Reuse the existing JSON-as-TEXT + `JSON_EXTRACT` pattern; no ORM.

### P0c — Config additions — S
`idc/config.py` (follow the `_env`/`has_*()` pattern, L15-101):
- `IDC_PRICING_URL` (Tencent Cloud pricing endpoint), `IDC_PRICING_OVERRIDE_PATH` (optional JSON `old→new` contract-price map), `has_pricing()`.
- `IDC_SMS_BASE/REGION/TOKEN` + `has_sms()` (defaults empty → F6 runs in "track-only" mode).
- `IDC_NETDEP_SOURCE` (`prometheus|zabbix|collector|off`, default `prometheus`) + `IDC_NETDEP_DAYS` (history window, default 7).
- Add all three to `.env.example`; reference in `doctor` (`cli/main.py` L611) so `idc doctor` reports their state.

**Phase 0 DoD**: `pytest` green; `idc doctor` reports the new settings; `reset` + `rebuild` still work with empty new tables.

---

## Phase 1 — Assessment completeness + funding case (F1, F2, F4)

Goal: bring the "assessment" half up to the three clouds. All three are
independent; do F1 and F4 in parallel, F2 after P0.

### F1 — Network dependency discovery — M
New adapter + normalize merge + plan integration.
- `idc/core/ingest/netdep.py` — new `NetdepAdapter(Adapter)` with
  `source = "netdep"` (register it; but note: `netdep` is *not* a CMDB
  source, it's a dependency signal — so do **not** route it through
  `run_ingest`'s raw-asset path. Instead expose a plain function
  `discover_network_deps(settings, servers) -> List[NetDep]` where
  `NetDep = (src_server_id, dst_ip, dst_port, observed_at, source)`).
  - Priority order from `IDC_NETDEP_SOURCE`: Prometheus
    `node_netstat_tcp`/`ss_estab` queries (reuse `prometheus.py`'s
    `_online` httpx pattern + `QUERIES` dict shape) → Zabbix
    `net.tcp.listen`/`net.tcp.port` items (reuse `zabbix.py` `item.get`+
    `history.get`) → optional future collector stub.
  - Offline/fixture: `fixtures/netdep.json` (a small sample) so it runs
    out-of-the-box like the other sources.
- `idc/core/normalize.py` — add `merge_network_deps(servers, netdeps)`:
  resolve `dst_ip` → `Server.id` via the existing `_key`/hostname-first
  identity (ServiceNow authoritative), emit `Workload.depends_on` edges
  (caller writes them back), and append a `source_refs` provenance entry
  with `source="netdep"`.
- `idc/core/__init__.py::rebuild` (L77) — after `resolve_workloads`, call
  `discover_network_deps` + `merge_network_deps`, then upsert workloads.
  Gated by `IDC_NETDEP_SOURCE != off`.
- `idc/core/plan.py::plan_waves` — the DAG already consumes
  `Workload.depends_on`; network edges flow through automatically. Add a
  field to `Wave.rationale` when a network-only edge was used (so it's
  visible in the UI / agent context).
- Tests: `tests/test_netdep.py` — fixture parse, ip→server resolution
  (incl. unresolved-dst case → flagged not dropped), merge dedup against
  existing `depends_on`, plan-wave rationale annotation.
**DoD**: a wave's dependency list can include edges that exist only in
network telemetry; unresolved dst_ips surface in a report, not silently
dropped.

### F4 — Data-coverage confidence rating — S
- `idc/core/models.py` — `Server.assessment_confidence` (P0a) +
  `Server.sizing_basis`.
- `idc/core/match.py::match_server` — after sizing, set `sizing_basis` =
  `"measured"` iff `Server.utilization` has cpu_p95+mem_p95 from a live
  source, else `"estimated"`. Compute `assessment_confidence`:
  `0.25*src_count + 0.35*util_history + 0.2*provenance_field_coverage +
  0.2*(1 if has_code_profile else 0)`, clamped [0,1]. Multiply into
  `Match.confidence` (the existing field): keep the original as
  `Match.rationale` note `"base_conf=… × coverage=…"`.
- `idc/core/codeintel.py::enrich_match` (L316) — already dips confidence
  for blockers/readiness; compose multiplicatively with
  `assessment_confidence` instead of overwriting.
- `web/assets/pages/inventory.js` + `server-drawer.js` — show a
  measured/estimated badge + coverage %; `web/assets/pages/dashboard.js`
  — add a "coverage distribution" tile.
- Tests: extend `test_pipeline.py` match tests for the two basis paths;
  add `test_confidence_coverage` for the weighting.
**DoD**: every Server row shows a coverage score + measured/estimated
label; match confidence reflects it.

### F2 — TCO / business case — L
The headline P0. Build on P0a/P0b.
- `idc/core/cost.py` (new) — pure functions, no DB:
  - `pricing()` loader: fetch Tencent Cloud public pricing
    (`IDC_PRICING_URL`) for CVM/CDB/Redis/CBS by region; cache in-memory
    (TTL via `computed_at`); apply `IDC_PRICING_OVERRIDE_PATH` override
    map (`old_sku→price`) for contract pricing. Returns a
    `PriceBook`.
  - `estimate_server(server, match, prices) -> CostEstimate`: pick
    monthly (pay-as-you-go) vs yearly (reserved) by `Server.env`/tags;
    IDC current cost from `Server.utilization` occupancy × a configurable
    on-prem rate (env var, default 0).
  - `estimate_portfolio(servers, matches, prices) -> Dict`: roll up by
    region / product / 6R-strategy; produce `annual_savings`,
    `per_server`, `per_strategy` arrays.
  - `what_if(servers, matches, prices, region=None, sizing=None,
    byol=None) -> Dict` — the pure function the F3 MigraQ will call; returns
    the delta vs the current estimate without re-running ingest.
- `idc/core/__init__.py::rebuild` — after `match_servers`, call
  `estimate_server` for each and `Store.upsert_cost`. Set
  `Match.basis`/`pricing_source`.
- Tag-driven retain/retire: `idc/core/codeintel.py::non_migrating_servers`
  (L82) already honors `AppStrategy`. Wire a tag convention
  `idc:retain` / `idc:retire` (read from `Server.tags`) into the
  strategies overlay so operators can mark hosts without a MigraQ call
  (à la Azure `AzM.MigrationIntent`). Add a helper
  `strategies_from_tags(servers)`.
- Backend `idc/backend/app.py` (new section after "7R strategy", ~L860):
  - `POST /api/business-case` → run `estimate_portfolio`, save a
    `business_case_snapshots` row, return the snapshot.
  - `GET /api/business-case` (latest) / `GET /api/business-case/{id}`.
  - `POST /api/cost/what-if` → calls `cost.what_if` (feeds F3).
- CLI `idc/cli/main.py`: `idc cost [--region] [--format json|csv]`,
  `idc business-case [--save]`. Mirror the `inventory` command's
  `--format` helper.
- Web: new `business-case` tab (`index.html` tab bar L13-19 + section;
  `web/assets/pages/business-case.js`) — exec summary tiles
  (annual savings, TCO delta), per-strategy breakdown table, per-server
  table with export. Reuse the existing `api()`/`toast`/`fmtUtil` helpers.
- Tests: `tests/test_cost.py` — pricing load + override, estimate for
  CVM/CDB/Redis, portfolio rollup, `what_if` delta sign, tag-driven
  retain/retire via `strategies_from_tags`.
**DoD**: `idc business-case` prints a portfolio TCO + per-strategy savings;
the web Business Case tab renders it; retain/retire via tag works end-to-end.

**Phase 1 DoD**: a fresh `ingest --source all && rebuild` produces, with no
extra config, a network-dependency-aware wave plan, per-server coverage +
measured/estimated labels, and a full business case.

---

## Phase 2 — Execution-state closure (F6, F7, F5, F3)

Goal: graduate from "assessment tool" to "migration copilot". F6 first
(others build on it), then F7 (UI on F6 data), F5 and F3 in parallel.

### F6 — Migration execution state machine + validation gates — L
- `idc/core/execute.py` (new) — the state machine, pure-ish:
  - `advance(job, to_status, store, agent=None) -> MigrationJob`:
    validates via `MIGRATION_JOB_TRANSITIONS` (P0a); on illegal transition
    raises `InvalidTransition`.
  - `revert(job, to_status)` — revert edges defined in the adjacency.
  - `run_validation_gates(job, settings) -> (ok, results)`: each gate is
    a declarative dict `{name, kind, must_pass}`. Gate `kind`s: `connect`
    (TCP port), `http_response`, `process_up`, `volume_consistency`,
    `app_health_promql`. The `app_health_promql` gate reuses
    `ingest/prometheus.py`'s httpx query. Gates run via the agent in
    plan mode (read-only) where they need filesystem/context, or direct
    HTTP otherwise.
  - `mark_complete(job, by)` — the manual/external path (operator clicks
    "complete" in UI when a 3rd-party tool did the migration).
- `idc/agent/executor_client.py` — no change for track-only; the SMS/DTS
  driver is Phase 2.5. For now `executor_ref` carries a `ChangeJob.id` for
  code-kind jobs (already works) or a free-string for external.
- Backend `idc/backend/app.py` (extend "pipeline ops" + "executor" sections):
  - `POST /api/waves/{wid}/execute` — create `MigrationJob`s for each
    member server (status `planned`).
  - `POST /api/migration-jobs/{id}/advance` `{to}` → `execute.advance`.
  - `POST /api/migration-jobs/{id}/revert` `{to}`.
  - `POST /api/migration-jobs/{id}/validate` → run gates, return results.
  - `POST /api/migration-jobs/{id}/complete` — manual mark-complete.
  - `GET /api/migration-jobs` (filter by wave/status) /
    `GET /api/migration-jobs/{id}`.
- CLI: sub-typer `migrate` (mirror `code_cli`): `idc migrate launch <wave>`,
  `idc migrate advance <job> --to`, `idc migrate validate <job>`,
  `idc migrate complete <job>`, `idc migrate jobs [--wave]`.
- Web: extend the `waves` tab with per-wave execution panel
  (`web/assets/pages/waves.js`) — server rows with status badges,
  advance/revert/validate/complete buttons; gate results inline.
- Tests: `tests/test_execute.py` — legal/illegal transitions, revert,
  each gate kind (mock httpx + prometheus), manual complete path.
**DoD**: a wave's servers progress through the state machine with
blocking validation gates; revert works at every stage; manual
mark-complete covers external tools.

### F6.5 — (optional, gated) Tencent SMS/DTS driver — L
Only if SMS API access is available. Adds `idc/core/execute_drivers/sms.py`
+ `dts.py`: `launch_replication(job)`, `cutover(job)` calling Tencent APIs.
Wire into `execute.advance` when `executor_ref.kind == "sms"`. Skip until
gated — track-only mode is shippable without it.

### F7 — Wave execution tracking dashboard — M
Mostly UI + one aggregation endpoint; data already lands in MariaDB via F6.
- `idc/core/plan.py` — add `wave_execution_status(wave_id, store) -> Dict`:
  aggregate member `MigrationJob` + `ChangeJob` states into a wave rollup
  `{planned, replicating, tested, ready_for_cutover, cut_over, finalized,
  rolled_back}` + per-server list. Wave.status becomes the rollup's
  dominant state.
- Backend: `GET /api/waves/{wid}/execution` (the rollup) + export
  `GET /api/waves/{wid}/handoff.csv` (wave×workload×target×tool×status —
  the AWS Migration-Hub→MGN handoff manifest, repurposed for Tencent SMS
  import + executor trigger).
- Web: promote the F6 execution panel into a full **Wave Execution** tab
  next to `waves` — rollup bar per wave, drill-down to servers,
  handoff-CSV export button, "mark complete" for manual flow.
- Tests: `tests/test_wave_execution.py` — rollup dominance, handoff CSV
  shape, manual+automated mix.
**DoD**: one screen shows portfolio-wide wave execution progress; the
handoff CSV is importable into the downstream runner.

### F5 — DB heterogeneous conversion assessment — L
Extends the executor contract + codeintel; does not require F6.
- `docs/agent-executor.md` — extend §1.2 scan categories with
  `plsql_compat, db_feature_gap, db_size_complexity` (the constants
  already land in P0a). Extend §1.3 with `DBConversionProfile` (sits
  alongside `CodeProfile` for DB-host apps): `db_server_id, source_engine,
  target_engine, difficulty` (A/B/C), `est_man_days, review_objects:List,
  blockers, reverse_replication: bool`. New push endpoint
  `PUT /api/db-profiles/{db_server_id}` + request `POST /v1/db-scan`.
- `idc/core/codeintel.py` — add `enrich_match_db(match, db_profile)`:
  hard conversion (C) → lower `Match.confidence`, set
  `migration_pattern=replatform`, append blocker to rationale. Add
  `db_conversion` into `wave_risk_basis` factors (L233).
- `idc/core/execute.py` (F6) — for `kind=db` jobs, the cutover revert path
  keeps the reverse-replication channel open until `finalized` (the F5
  reverse-replication safety net).
- Mock executor `idc/executor_mock/app.py` — implement `/v1/db-scan` +
  push back so the loop is testable end-to-end without the real executor.
- Tests: `tests/test_db_conversion.py` — profile roundtrip, A/B/C →
  confidence dip + pattern change, reverse-replication flag flows to F6.
**DoD**: an Oracle host shows a conversion-difficulty grade + man-day
estimate + blocker; the wave planner defers hard-DB hosts.

### F3 — Conversational what-if — M
Reuses F2's `cost.what_if` + match pure functions.
- `idc/core/match.py` — refactor `match_server` to expose
  `right_size(server, strategy="measured"|"as_is")` and
  `re_region(server, region)` as pure functions (the current inline
  sizing/region logic moves into these). `match_server` becomes a thin
  caller. This is a pure refactor — `test_pipeline.py` must stay green.
- `idc/llm/planner.py` (or `client.py`) — register what-if tools the
  agent can call: `cost_what_if(region, scope)`, `right_size(server_id,
  strategy)`, `re_region(server_id, region)`. Each returns a structured
  delta the MigraQ narrates. The MigraQ/agent layer already exists; this only
  adds tool definitions.
- Backend: `POST /api/cost/what-if` (already in F2) — expose it to the
  `copilot` tab as a tool the agent uses; add a `copilot`-tab "What-if"
  panel that calls it directly too.
- Tests: extend `test_cost.py` for `what_if`; add `test_whatif_tools.py`
  for the agent tool wiring (mock the underlying functions).
**DoD**: ask the copilot "what if all DBs go to ap-guangzhou?" and get a
cost delta without re-running ingest.

**Phase 2 DoD**: an operator can launch a wave, watch servers advance
through replication → test → cutover with blocking validation gates,
revert on failure, and export a handoff manifest — with DB hosts carrying
conversion-difficulty signals and the copilot answering what-if cost
questions. Phase 2.5 adds the real SMS/DTS driver when API access lands.

---

## Phase 3 — Scale + modernization (F8, F9, F10)

### F8 — Landing Zone parameterized blueprint + policy-as-code — M
- `idc/agent/claude_runner.py` — add LZ archetype templates (`corp`,
  `online`, `dmz`) as agent context blocks mapping to Tencent Cloud
  VPC/peering/CAM/SG/tag-policy. The agent already emits Terraform; this
  parameterizes it.
- `idc/core/plan.py` — `archetype_for(server, match) -> str` and a
  placement gate: a workload's wave requires the target archetype's LZ
  wave-1 to be `finalized` (plugs into F6 + F10).
- Web `waves` tab: show archetype per server + a "LZ readiness" gate.
- Tests: `tests/test_lz_archetype.py`.
**DoD**: LZ Terraform is emitted per archetype; wave launch is gated on
the matching archetype being ready.

### F9 — Runtime containerization (no-source path) — M
- `docs/agent-executor.md` — new action `runtime-containerize`:
  input = Zabbix/Prometheus-discovered process+port+software inventory for
  an app (no repo); output = a `CodeProfile` tagged
  `source=runtime-derived` + a Dockerfile scaffold `patch_ref`.
- `idc/agent/executor_client.py` — `runtime_containerize(app_id,
  server_id, mode="plan")`.
- `idc/core/codeintel.py` — `runtime-derived` profiles get
  `confidence` capped lower and force a `Question` (operator confirm)
  before `modify` (reuses the existing question loop).
- Mock executor + tests: `tests/test_runtime_containerize.py`.
**DoD**: a source-less legacy app gets a Dockerfile scaffold + an operator
confirm-gate, without a repo URL.

### F10 — Portfolio readiness heatmap — M
- `idc/core/readiness.py` (new) — `wave_readiness(wave_id, store) -> Dict`:
  per-wave scores across {LZ ready (F8), DB conversion done (F5), code
  refactor done (executor ChangeJob `done`), deps resolved (F1 net+code),
  cutover rehearsal passed (F6 `tested`), rollback channel ready}, each
  green/yellow/red; wave rollup = worst case (red blocks cutover).
- Backend: `GET /api/readiness`, `GET /api/readiness/{wave_id}`.
- CLI: `idc readiness [--wave]`. Web: a **Readiness** tab with the heatmap.
- Tests: `tests/test_readiness.py` — each signal path + worst-case rollup.
**DoD**: one heatmap answers "is this wave safe to cut?".

---

## Cross-cutting

- **All code in English** (identifiers, comments, docstrings, commit
  messages) — project rule, see memory `code-in-english`.
- **Out-of-the-box**: every new ingest path (F1 netdep, F9 runtime) ships
  a `fixtures/` sample + an off-by-default gate so `ingest && rebuild`
  works with zero creds, matching the existing source contract.
- **Tests first**: each F-item's DoD is a passing `tests/test_<feature>.py`
  following the existing `monkeypatch` + `TestClient` + pure-function
  patterns (see `test_codeintel.py` / `test_executor_endpoints.py`).
- **No MigraQ in the loop for deterministic work**: F1/F2/F4/F6/F7/F10 are
  deterministic (rules + telemetry + state machine); the MigraQ/agent only
  drives F3 (what-if narration), F5 (DB conversion fallback), F8 (Terraform
  emit), F9 (scaffold gen). Keeps the copilot reproducible and cheap.
- **Dep graph**: P0 → {F1, F4} ‖ F2; F2 → F3; F6 → F7; F5 ‖ F3 (both after
  F2/F6); F6.5 gated; F8 → F10; F9 independent. Phase 3 items are
  independent of each other.

## Suggested sequencing (one dev)

1. P0a + P0b + P0c (foundation) →
2. F4 (S, quick win) ‖ F1 (M) →
3. F2 (L, the headline) →
4. F6 (L) → F7 (M) →
5. F5 (L) ‖ F3 (M) →
6. Phase 2.5 SMS/DTS if available →
7. F8 → F10 → F9 (Phase 3, lower urgency).