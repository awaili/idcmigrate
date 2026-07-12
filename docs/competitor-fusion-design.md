# Competitor Fusion Design — IDC→Tencent Cloud Migration Copilot

> Benchmark: the "migration & modernization" tool stacks of GCP / AWS /
> Azure (as of 2026-07, product names verified). Goal: fuse the
> capabilities we **lack and that materially matter for a 15K-server /
> 200K-core migration copilot** into idc-migrate.
>
> Principle: **do not rebuild what we already do well** — multi-source
> entity resolution + provenance, code-aware wave planning, the external
> executor (evidence-backed findings + operator resolve/question loop),
> and the MigraQ/Agent dual layer already match or lead the competitors; keep
> them. **Only fill gaps**, prioritized by "impact on migration outcome ×
> implementation cost".
>
> Competitor name notes (verified): AWS's entire Migration Hub family
> (Hub / Strategy Recommendations / Orchestrator / Refactor Spaces) was
> closed to new customers 2025-11 and rolled into **AWS Transform**;
> MGN → **AWS Transform MGN** (2026-06). Azure retired **DMA** and
> **.NET Upgrade Assistant** in favor of **GitHub Copilot
> modernize-dotnet** + SSMS Migration Component. GCP's stack is
> **Migration Center** (ex-StratoZone) + **Migrate to Virtual Machines**
> (ex-Velostrata) + **Migrate to Containers** (ex-Migrate to Anthos).
> GCP has **no native wave tracker** (only the manual RaMP Google Sheet)
> — a point where we are already at parity or ahead.

---

## 1. Capabilities we already lead / match (keep, do not copy)

| Our capability | Competitor equivalent | Verdict |
|---|---|---|
| 4-source ingest + cross-source entity resolution + per-field provenance | AWS ADS/Transform, GCP Migration Center, Azure Migrate appliance (mostly single-source appliance) | Our multi-source fusion is stronger; **but we lack network-dependency discovery** → F1 |
| Code-aware wave planning (DAG + code_deps + refactor_effort / migration_pattern secondary sort) | GCP has **no** native wave tracker; AWS Migration Hub / Azure Wave Planning have grouping but not code-aware | At parity / ahead; **but lacks execution-state tracking** → F7 |
| External executor: file+line+evidence findings, `modify` actually edits code, operator resolve/question loop | AWS Strategy Recommendations does only static anti-pattern detection, no edit; AWS Transform code refactor has no operator loop; Azure has no equivalent | **Leading** — keep, extend to DB → F5 |
| MigraQ ask/explain + Agent (Claude CLI) plan/execute dual mode | AWS Transform chat what-if; Azure Copilot migration agent (preview) | At parity, **deepen what-if** → F3 |

---

## 2. Capabilities to fuse (by priority)

### F1 — Network dependency discovery (agentless dependency mapping) ★P0
**Source**: AWS ADS/Transform, GCP Migration Center, Azure Migrate — all three
do agentless TCP-connection polling to build dependency graphs.
**Gap**: our app DAG is static + executor's `code_deps`; **no runtime network
dependency**. All three clouds treat this as assessment-standard — it is the
primary defense against "left something behind" and "cut the wrong wave".
**Fusion**: add `idc/core/ingest/netdep.py`:
- Source priority: Prometheus `node_netstat`/`netstat_tcp` (ingest hook
  already exists) → Zabbix `net.tcp` / host network items → optional
  fallback lightweight agentless collector (TCP-connection snapshot,
  analogous to Azure appliance's 5-min poll).
- Emit `(src_server, dst_ip:port, observed_at)` triples; `normalize.py`
  resolves `dst_ip` back to a Server (reuse existing entity resolution),
  merges into `Workload.depends_on`, deduped against `code_deps`.
- `plan.py` topological sort consumes static edges + code edges +
  **network edges**; on disagreement, the wave rationale flags
  "new dependency observed on the network".
**Landing**: `idc/core/ingest/netdep.py` (new), `normalize.py`, `plan.py`.
**Why P0**: it is the root of assessment completeness, and we already have
the Prometheus/Zabbix ingest hooks, so the incremental cost is low.

### F2 — TCO / business case + 6R strategy upgrade ★P0
**Source**: AWS Migration Evaluator / Transform (BYOL vs License-Included,
3-year run cost, PDF business case); GCP Migration Center TCO (up to 4
preference sets side-by-side); Azure Business Case (TCO/NPV/CO2e + 5R
strategy).
**Gap**: we have `migration_pattern` (rehost/replatform/refactor/rewrite,
from executor) but **no cost/TCO, no portfolio business case, missing
retain/retire/repurchase**.
**Fusion**:
- `match.py` already yields `(product, spec, region)` → add
  `idc/core/cost.py`: consume Tencent Cloud pricing (CVM/CDB/Redis/CBS —
  pay-as-you-go + monthly/yearly + RI/reserved-deduction), compute monthly/
  yearly run cost per region; IDC current cost estimated from Zabbix
  utilization (actual occupancy).
- Extend `migration_pattern` from 4R to **6R**: add `retain` (do not
  migrate, compliance/edge), `retire` (decommission), `repurchase` (move to
  SaaS / Tencent managed service). retire/retain are **tag-driven** (à la
  Azure `AzM.MigrationIntent:Retain`), not heuristic — preserves explicit
  operator override.
- Business case artifact: exec summary (annual savings) + per-server
  breakdown + per-strategy rollup, export xlsx/md; add a "Business Case"
  tab to the web UI.
**Landing**: `idc/core/cost.py` (new), `match.py` (extend pattern enum),
`models.py` (CostEstimate field), `web/index.html`, `cli/main.py`
(`idc cost` / `idc business-case`).
**Why P0**: the business case is the hard gate for migration funding;
without it the copilot stalls at the "technical assessment" layer.

### F3 — Conversational what-if refinement (no ingest re-run) — P1
**Source**: AWS Transform "chat-based refinement" — adjust
region/licensing/utilization in natural language without re-running the
assessment; Azure Copilot migration agent (preview).
**Gap**: MigraQ `ask` answers from a precomputed snapshot; changing region or
spec requires re-running `match`.
**Fusion**: wrap `match.py` rule tables + `cost.py` as parameterized pure
functions (region / sizing-strategy / BYOL as inputs); the MigraQ agent calls
them for what-if:
- "If I move all DBs to ap-guangzhou, what's the cost delta?" → agent calls
  `cost.what_if(region='ap-guangzhou', scope='db')`, returns the delta.
- "Re-right-size this host as-is" → `match.right_size(server_id, strategy='as_is')`.
**Landing**: `match.py` + `cost.py` (extract pure functions),
`idc/agent/` (what-if tool set), `llm/planner.py`. Reuses the existing
MigraQ/Agent layer; the increment is mostly function-izing match/cost.

### F4 — Data-coverage confidence rating — P1
**Source**: Azure Migrate's 1–5 star confidence (by utilization data-point
coverage); AWS Transform's estimate-vs-actual transparency; GCP TCO's
"which assets used estimates" flag.
**Gap**: we have `match.confidence` and field-level provenance, but no
overall "how trustworthy is this server's assessment" score, and no
distinction between "measured sizing vs estimated sizing".
**Fusion**: add `assessment_confidence ∈ [0,1]` per Server:
- Weighted from utilization-history length (Zabbix history / Prometheus
  range) × source count × key-field provenance coverage × whether an
  executor CodeProfile exists.
- `final_confidence = match.confidence × assessment_confidence`.
- UI explicitly labels "measured / estimated" (à la GCP per-asset sizing
  transparency).
**Landing**: `models.py`, `match.py`, `web/index.html`. Fits our provenance
model naturally; light to implement.

### F5 — DB heterogeneous conversion assessment (Oracle/SQLServer → TDSQL/CDB) ★P1
**Source**: AWS DMS Schema Conversion (GenAI-assisted, ~90% auto-conversion,
quality scoring); GCP DMS (Gemini conversion + pattern learning +
**reverse replication**); Azure (Ora2Pg difficulty grade A–C + man-day
estimate + Foundry AI conversion).
**Gap**: `match.py` only labels Oracle→CVM(BYOL)/TDSQL as targets; it does
**no schema/SQL conversion assessment**, and blockers carry no
"PL/SQL conversion difficulty" signal.
**Fusion**: extend the executor's scan categories from app code to **DB
schema/SQL**:
- New finding categories: `plsql_compat`, `db_feature_gap` (Oracle-specific
  syntax/packages → MySQL equivalent), `db_size_complexity`.
- Produce a `DBConversionProfile`: conversion-difficulty grade (à la Ora2Pg
  A–C), man-day estimate, manual-review object list, blockers.
- AI-assisted conversion (via our MigraQ/Agent): rule engine converts first,
  MigraQ handles stored procedures/triggers, **with a quality score** (à la
  DMS SC); flows back into `codeintel.py` to influence `migration_pattern`
  (hard DB conversion → replatform/refactor rather than direct rehost).
- **Reverse replication** (à la GCP DMS): keep a write-back channel during
  the cutover window as a rollback safety net (feeds F6).
**Landing**: `agent/executor_client.py` (extend scan categories),
`core/codeintel.py` (DBConversionProfile), `docs/agent-executor.md`
(interface extension).
**Why P1**: DB is the most expensive, highest-risk migration segment;
all three clouds invest heavily here and we currently have nothing.

### F6 — Migration execution state machine + declarative post-launch validation gates ★P1
**Source**: AWS MGN's **Test → ReadyForCutover → Cutover → Finalize +
revert** + SSM post-launch actions (failure blocks cutover); Azure test
migration (sandbox VM, repeatable, non-destructive) +
Preparation→Testing→Completion three stages; GCP test-clone (clone from
the latest replication cycle).
**Gap**: we **plan down to waves and edit code, but do not track / orchestrate
the actual migration execution**. The executor has a ChangeJob state machine,
but **host-level migration** (lift-and-shift, DB migration) has no
equivalent lifecycle, no cutover gate, no validation gate.
**Fusion**: introduce `MigrationJob` as a first-class object with the
state machine:
```
planned → replicating → tested → ready_for_cutover → cut_over → finalized
            ↑—————————————————— revert ——————————————————↑ (revertible at any stage)
```
- Host-level: integrate Tencent Cloud **SMS (Server Migration Service)**
  for replication + cutover; DB-level: integrate **DTS**.
- **Post-launch validation gates** (à la AWS SSM post-launch actions,
  declarative, failure blocks finalize): connectivity check, port
  response, volume consistency, process liveness, app health (reuse the
  existing `ingest/prometheus.py` metrics hook).
- Cutover reverse channel (F5 reverse replication) as the rollback net.
**Landing**: `core/models.py` (MigrationJob), `core/execute.py` (new,
state machine + validation gates), `agent/executor_client.py` (reuse +
extend ChangeJob), `backend/app.py`, `web/index.html`.
**Why P1**: without execution state, the copilot is an "assessment tool",
not a "migration copilot".

### F7 — Wave execution tracking dashboard — P1
**Source**: AWS Migration Hub (app→wave grouping + cross-MGN/DMS status
aggregation + CSV-into-MGN handoff); Azure Wave Planning (automated/manual
flows + ARG-queryable + Power BI); GCP (gap — RaMP manual Sheet).
**Gap**: we have wave **planning**, not wave **execution progress**.
**Fusion**:
- Each wave aggregates the `MigrationJob` states of all its Servers + the
  `ChangeJob` states of all its apps → wave-level progress
  (planned/replicating/tested/cutover-ready/done).
- **Automated flow**: MigrationJob/ChangeJob drives status automatically;
  **manual flow** (third-party / human migration) gets operator
  "mark complete" (à la Azure).
- Data already lives in MariaDB = a natural ARG equivalent → web UI
  "Wave Execution" tab + REST query; export a **migration handoff
  manifest** (à la AWS Hub→MGN CSV handoff): a wave×workload×target×tool×
  status CSV for Tencent SMS import + executor trigger.
**Landing**: `core/plan.py` (wave status aggregation), `backend/app.py`,
`web/index.html`. Depends on F6, but the UI/query increment is small
(data already in MariaDB).

### F8 — Landing Zone as a parameterized blueprint + policy-as-code — P2
**Source**: AWS Control Tower v4 + LZA (toggleable controls, flexible OU);
Azure ALZ (platform+application LZ, archetype-driven, policy auto-deploys
spokes); GCP Foundation Fabric.
**Gap**: wave 1 = LZ + agent-generated Terraform, but it is **one-shot
generation with no archetype / policy gate**.
**Fusion**:
- Modularize the LZ into archetypes (à la Azure Corp/Online/DMZ):
  `corp` (hybrid connectivity), `online` (internet-exposed), `dmz`
  (isolated) → each maps to Tencent Cloud VPC / peering / CAM / security
  group / tag-policy templates.
- Agent emits a per-archetype Terraform manifest (à la Control Tower
  toggleable controls + LZA compliance baseline); CAM policies + tags act
  as "workload placement gates" (à la Azure `Deploy-VNET-HubSpoke` policy
  auto-deploying spokes).
- When a workload enters a wave, verify "the target LZ archetype is
  ready" (feeds F10 readiness).
**Landing**: `idc/agent/` (LZ blueprint templates), `core/plan.py`
(archetype validation). We already generate Terraform; this extends it
into a parameterized blueprint.

### F9 — Runtime containerization (no-source path) — P2
**Source**: GCP Migrate to Containers (VM→container artifact, **no source
code required**); Azure App Containerization (generate Dockerfile from
runtime state, ASP.NET/Java).
**Gap**: the executor scans source; **legacy apps without source**
(vendor packages, binary-only) are not covered.
**Fusion**: add a "no-source containerization" path:
- From Zabbix/Prometheus-discovered process + port + software inventory,
  generate a Dockerfile scaffold (à la Azure/GCP inferring components
  from runtime state).
- The resulting `CodeProfile` is tagged `source=runtime-derived,
  confidence<source-derived` and requires operator confirmation (reuses
  the existing question/answer loop).
**Landing**: `agent/executor_client.py` (new action
`runtime-containerize`), `docs/agent-executor.md`. Fills the executor's
coverage blind spot; scope depends on customer source availability.

### F10 — CAF / migration-readiness heatmap (portfolio readiness scorecard) — P2
**Source**: AWS CAF MRA (24 readiness-area heatmap); Azure CAF
(Strategy→Plan→Ready→Adopt→Govern); GCP CAF (Learn/Lead/Scale/Secure +
Assess→Plan→Deploy→Optimize).
**Gap**: we have `doctor` (**tool-self** readiness: config/MigraQ/claude),
not **estate readiness** ("is this wave actually safe to cut?").
**Fusion**: portfolio readiness scorecard, per wave:
- LZ readiness (F8), DB conversion completeness (F5), code refactor
  completeness (executor CodeProfile + ChangeJob), dependency resolution
  completeness (F1 network + code), cutover rehearsal passed (F6 tested),
  rollback channel ready.
- Heatmap green/yellow/red, red = cannot enter cutover (à la AWS MRA +
  Azure CAF).
**Landing**: `core/readiness.py` (new), `web/index.html`, `cli/main.py`
(`idc readiness`). Collapses scattered signals into a "safe to cut?"
decision panel.

### F11 — Low priority / optional (backlog)
- **Sustainability / CO2e** (both Azure and AWS business cases include it):
  low value in the domestic IDC→Tencent Cloud context; backlog.
- **Strangler-Fig microservice extraction** (AWS Refactor Spaces, closed
  2025-11): high complexity, narrow audience; not in scope now.

---

## 3. Roadmap (3 phases)

| Phase | Capabilities | Goal |
|---|---|---|
| **Phase 1 · assessment completeness + funding case** | F1 network deps, F2 TCO/6R/business case, F4 confidence rating | Bring the "assessment" half up to the three clouds |
| **Phase 2 · execution-state closure** | F6 migration execution state machine + gates, F7 wave execution dashboard, F5 DB conversion assessment, F3 what-if | Graduate from "assessment tool" to "migration copilot" |
| **Phase 3 · scale + modernization** | F8 LZ blueprint, F9 runtime containerization, F10 readiness heatmap | Scale and modernization coverage |

---

## 4. Explicitly not copied

- A single-appliance discovery — our multi-source fusion + provenance is
  stronger; we only borrow its "network dependency" piece (F1).
- GCP's RaMP manual Sheet — our data is in MariaDB, F7 ships a dashboard
  directly.
- Azure ASR as the migration engine — Tencent Cloud has SMS; F6
  integrates SMS rather than building a replication engine.
- AWS Refactor Spaces — closed, high complexity, narrow audience.

---

## Appendix: competitor key product names (verified 2026-07)

| Stage | AWS | GCP | Azure |
|---|---|---|---|
| Discovery / assessment | Application Discovery Service → **AWS Transform** | **Migration Center** (ex-StratoZone) | Azure Migrate appliance + Discovery and assessment |
| TCO / strategy | Migration Evaluator → Transform; Strategy Recommendations → Transform (closed) | Migration Center TCO (6R) | Business Case (5R + NPV/CO2e) |
| Host migration | **AWS Transform MGN** (ex-MGN / ex-CloudEndure) | Migrate to Virtual Machines (ex-Velostrata) | Migration and modernization tool (ex-Server Migration) |
| DB migration | DMS + DMS Schema Conversion (GenAI) + SCT | DMS (Gemini conversion + reverse replication) | Azure DMS + SSMS Migration Component (ex-DMA) + Ora2Pg |
| Code modernization | AWS Transform / Q Developer Transform (Java/.NET/mainframe) | Migrate to Containers (ex-Migrate to Anthos) | GitHub Copilot modernize-dotnet + App Containerization |
| Wave tracking | Migration Hub → Transform | **gap** (RaMP Sheet) | Wave Planning (preview) + ARG |
| Landing zone | Control Tower v4 + LZA | Cloud Foundation Fabric | Azure Landing Zones + ALZ Accelerator |
| Pre-cutover | MGN Test/Cutover + SSM validation gates | test-clone | Test migration (sandbox VM) |