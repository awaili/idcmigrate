# External Agent Executor — Interface Spec & Requirements

idc-migrate outsources "code scan / comb / modify" to an **external agent executor** (hereafter *executor*). The executor scans application source repos, produces migration assessments, and modifies code on demand; assessment results are pushed back to idc-migrate as an extra signal for **reconsolidate, wave planning, and migration strategy**.

This document defines two things:

1. **Requirements for the executor** — what it must be able to do, the scan categories, output obligations, and quality bar.
2. **Interface spec** — the two-way REST contract, schemas, auth, lifecycle, and webhooks.

> Data models live in `idc/core/models.py`: `CodeProfile` (per-app code assessment), `ScanFinding` (a single finding), `ChangeJob` (audit record for each scan/comb/modify run).

---

## 1. Requirements for the executor

### 1.1 The three actions

| Action | Input | Output |
|---|---|---|
| **scan** | `app_id`, `repo_url`, `branch` (optional) | a set of `ScanFinding` (classified per §1.2) |
| **comb** | scan findings + the repo | a `CodeProfile`: cloud_readiness, migration_pattern, refactor_effort, code_deps, required_changes, blockers, summary |
| **modify** | `changes` (the concrete change list idc-migrate assembles from the CodeProfile + operator overrides) + the repo + authorization | actual code changes: parameterize IPs, swap service discovery, containerize, externalize config…; produces a `patch_ref` (commit/PR/artifact) |

scan is read-only; comb is read-only analysis; **only modify has side effects** — it requires explicit authorization (`mode=execute`) and is dry-run by default.

### 1.2 Scan categories (must be covered; output uses these `category` values)

`hardcoded_ip` · `service_discovery` · `db_connection` · `stateful_local` · `scheduled_job` · `secrets_in_repo` · `baremetal_assumption` · `network_dependency` · `os_dependency` · `legacy_runtime` · `config_coupling`

**F5 — DB schema/SQL conversion categories** (used during heterogeneous-DB assessment, see §1.5): `plsql_compat` (Oracle PL/SQL constructs with no MySQL equivalent) · `db_feature_gap` (engine-specific feature gaps: sequences, partitions, materialized views…) · `db_size_complexity` (schema scale / object count driving conversion cost).

Each `ScanFinding` has fields: `category`, `severity` (`low|medium|high|blocker`), `file` (repo-relative path), `line`, `message`, `evidence` (truncated code snippet / matched token), `remediation`.

### 1.3 Output obligations (required CodeProfile fields)

- `cloud_readiness` ∈ [0,1]: derived from the severity and count of findings. A `blocker`-level finding must push readiness below 0.5.
- `migration_pattern` ∈ {`rehost`,`replatform`,`refactor`,`rewrite`}: a blocker that cannot be fixed → `rewrite`; needs connection/service-discovery changes but the skeleton is reusable → `replatform`; moderate changes → `refactor`; pure lift-and-shift → `rehost`.
- `refactor_effort` ∈ {`low`,`medium`,`high`}: estimated from the count and risk of required_changes.
- `code_deps`: dependencies on other `app_id`s **measured** from the code (HTTP/RPC/message/shared-library calls), used to complete the application dependency graph. This is the most important incremental signal for reconsolidate/waveplan.
- `blockers`: hard blockers preventing direct migration (plain-language; goes into the match rationale).
- `required_changes`: each item `{title, category, file, effort, description}`.
- `scanned_at` / `scanner` (executor name + version) / `scan_id` (the job id that produced this profile).

### 1.4 Quality bar

- **Traceable**: every finding must carry `file`+`line`+`evidence`; no unlocated, vague conclusions.
- **Determinism first**: rule-based checks must be stable and reproducible; only items needing semantic judgment use MigraQ, and MigraQ conclusions must land on an evidenced finding.
- **Idempotent**: re-running the same `(app_id, repo_url, branch)` upserts the `CodeProfile` (overwrites by `app_id`) — no duplicates.
- **Truncation**: `evidence` ≤ 512 chars; a single app's `findings` should be capped at 500, keeping the top by severity when exceeded.
- **Observable failures**: an execution error must return `ChangeJob.status=error` + an `error` message; never swallow it silently.
- **Read-only by default**: `modify` is dry-run by default (produces only a `patch_ref`, writes nothing to the repo); writing to the repo requires the caller to pass `mode=execute`.

### 1.5 Heterogeneous-DB conversion assessment (F5 — fourth action: db-scan)

For **database hosts** (`Server.role == "db"` whose source engine is Oracle/SQLServer), the executor adds a `db-scan` action: scan the DB schema/SQL and produce a `DBConversionProfile` (see `idc/core/models.py`). It sits alongside `CodeProfile` but is aggregated by the **DB host's stable identity** (hostname — not the volatile `Server.id`, which changes on every rebuild).

`DBConversionProfile` fields:

- `db_server_id`: the DB host's stable identity (hostname lowercased). **This is the primary key**; the executor reports with it.
- `source_engine` / `target_engine`: `oracle` / `sqlserver` / `mysql` → `tdsql` / `cdb_mysql`.
- `difficulty` ∈ {`A`,`B`,`C`}: conversion difficulty grade (mirrors Ora2Pg A–C / AWS DMS SC quality score). `A`=nearly fully auto-convertible, `B`=a few objects need manual review, `C`=heavy PL/SQL / engine-specific features, requires replatform.
- `est_man_days`: estimated manual conversion person-days.
- `review_objects`: list of objects needing manual review (packages/triggers/views).
- `blockers`: hard blockers (plain-language; goes into the match rationale).
- `reverse_replication`: whether to keep a reverse-replication channel during the cutover window (rollback safety net; F6 db-kind tasks keep this channel until `finalized`).
- `auto_convert_pct` ∈ [0,1]: fraction the rule engine can auto-convert (mirrors DMS SC).
- `scan_id` / `scanned_at` / `summary`.

**How idc-migrate uses it** (next `rebuild`): `codeintel.enrich_match_db` adjusts the DB host's `Match` by difficulty — grade `C` strongly lowers confidence + forces `replatform` (a direct rehost would carry unconverted PL/SQL) + writes blockers into the rationale; grade `B` slightly lowers confidence; grade `A` doesn't lower, just marks it assessed. `wave_risk_basis` raises the risk of waves containing grade-`C` DB hosts. `reverse_replication=True` keeps the channel open in the F6 db-kind cutover rollback path until `finalized`.

### 1.6 Legacy / unsupported-OS disposition (F7 — sixth action: legacy-disposition)

For **EOL / unsupported-OS hosts** (`os_eol_bucket ∈ {expired, expiring}`), the executor adds a `legacy-disposition` action: analyze the workload and recommend one of **containerize / re-platform / rewrite / retain** (replacing the previously hardcoded "replatform to a supported base image" in `eol.apply_os_eol`).

Input = workload context (the executor uses it to distinguish a stateless web tier with no source from a tightly-coupled legacy DB):

```json
{"server_id": "web-1", "context": {
   "os": "centos 6", "os_eol_bucket": "expired", "runtime": "jdk7",
   "role": "app", "criticality": "high", "has_source_repo": false,
   "runtime_inventory": {"process": "java -jar app.jar", "port": 8080,
                         "software": ["openjdk-7"]},
   "code_profile_summary": "..."}}
```

Output = `LegacyDisposition` (`idc/core/models.py`), pushed back by **host stable identity** (hostname lowercased) to `PUT /api/legacy-dispositions/{server_id}`:

```json
{"server_id": "web-1", "disposition": "containerize",
 "rationale": "stateless web tier, no source repo -> Dockerfile scaffold",
 "confidence": 0.7, "target_base_image": "tencentos-server-3.1:jdk17",
 "effort_days": 5.0, "prereqs": ["capture runtime inventory", "validate port 8080"],
 "scan_id": "cjob-...", "scanned_at": "...", "summary": "containerize"}
```

`disposition ∈ {containerize, replatform, rewrite, retain}`. `retain` counts as a non-migration (goes into the trailing wave, same as retain/retire); the other three are migrations (rewrite goes into a later wave).

**How idc-migrate uses it** (next `rebuild`): `apply_os_eol` reads the stored `LegacyDisposition` by hostname and writes its `disposition + rationale` into the match's `alternatives` (replacing the hardcoded string); the confidence dip is still driven by the OS bucket (deterministic, unchanged). With no stored disposition it falls back to the original fixed string (backwards-compatible).

### 1.7 Cutover playbook / as-built documents (F9 — seventh & eighth actions: cutover-playbook + as-built)

The executor adds two document-generation actions that produce **grounded markdown** from the real data idc-migrate sends (so idc-migrate doesn't have to run its own LLM for these). The existing per-wave `assess_wave` runbook JSON is also persisted as a `DocArtifact(doc_type=runbook, scope_id=wave_id)`, so every wave has a downloadable doc (closing the "runbook produces no document" gap).

`POST /v1/cutover-playbook` — input: the wave's members (server/role/target/ports/deps), the downtime window, and the deterministic risk basis:

```json
{"wave_id": "w-3", "context": {
  "members": [{"server_id": "db-1", "role": "db", "target": "CDB",
               "reverse_replication": true, "ports": [3306]}],
  "downtime_window": {"start": "2026-07-20T02:00Z", "end": "2026-07-20T04:00Z"},
  "risk_basis": {"score": 7, "level": "high"}}}
```

`POST /v1/as-built` — input: the executed wave's `stage_history`, gate results, change jobs, and targets. Both push a `DocArtifact` (markdown) back to `PUT /api/docs/{cutover|as-built}/{wave_id}`:

```json
{"doc_type": "cutover", "scope_id": "w-3",
 "doc_md": "# Cutover playbook — wave w-3 ...",
 "scan_id": "cjob-...", "scanned_at": "...", "summary": "cutover for w-3"}
```

`doc_type ∈ {runbook, cutover, as_built}`, upserted by the composite key `(doc_type, scope_id)`. The URL slug is `as-built` (URL-friendly); the stored `doc_type` is `as_built`. idc-migrate auto-triggers cutover-playbook when a wave enters `ready` and as-built when it enters `finalized`; the CLI `idc doc cutover|as-built <wave>` triggers manually, `idc doc show <type> <wave>` prints it, and the web renders + exports `.md`.

### 1.8 No-source containerization (F9 — fifth action: runtime-containerize)

For legacy apps **without a source repo** (vendor packages / pure binaries), the executor adds a `runtime-containerize` action: infer a Dockerfile scaffold from the **runtime inventory** discovered by Zabbix/Prometheus (process + port + software list) and produce a `CodeProfile` (same shape as §1.3) with its `source` field set to `runtime-derived` (instead of the default `repo`).

Field constraints:
- `source ∈ {"repo", "runtime-derived"}`: the profile's origin. `runtime-derived` means inferred from runtime telemetry (no source); its confidence is capped at 0.5, and `modify` in `execute` mode must first pass an operator confirmation (a `Question`, see below).
- The Dockerfile-scaffold change in `required_changes` has `file="Dockerfile"`; `patch_ref` points at the inferred scaffold.
- `findings` record the inference evidence (`process=`/`port=`/`unit=`).

**Confirm gate**: a `runtime-derived` profile is a scaffold *inference*, not a scan. Before `modify` `mode=execute` writes, idc-migrate raises a `Question` (`kind=choice`, options `confirmed — proceed` / `reject — needs manual review`); only after the operator answers can `modify` be re-triggered. `mode=plan` (dry-run scaffold preview) is not gated.

### 1.9 IaC generation + Well-Architected guardrails (F5 — ninth action: iac-emit)

The executor emits **structured IaC modules** (Tencent Cloud Terraform HCL) and runs **Well-Architected guardrail checks** (the `LZ_BLUEPRINTS.policy_as_code` strings become the rule ids the executor evaluates). idc-migrate stores the artifact + check results; `check_lz_gate` **blocks workload wave launch** when the target archetype's latest `lz:<arch>` artifact fails its guardrails — a real guardrail evaluation replacing the old gate that only looked at the operator's finalized flag.

`POST /v1/iac-emit`, with `scope=landing_zone` (`scope_id=lz:<arch>`, `context=blueprint`) or `scope=workload` (`scope_id=wl:<server_id>`, `context=match`). Pushes an `IaCArtifact` back to `PUT /api/iac-artifacts/{scope_id}`:

```json
{"scope_id": "lz:corp", "scope": "landing_zone",
 "modules": [{"path": "main.tf", "content": "resource \"tencentcloud_vpc\" ..."}],
 "guardrails": [
   {"pillar": "security", "rule": "no_public_clb_in_corp", "status": "pass", "finding": "", "severity": "high"},
   {"pillar": "cost", "rule": "required_tags", "status": "fail",
    "finding": "vpc-corp missing cost_center tag", "severity": "medium"}],
 "guardrail_pass": false, "plan_summary": "Plan: 2 to add, ...",
 "target": {"cloud": "tencent", "region": "ap-bangkok"}}
```

`guardrail_pass` = no `status=fail` with `severity ∈ {high, medium}`. With no artifact, it falls back to the finalized flag (the guardrail only adds a gate; it never weakens the original one).

### 1.10 Post-migration optimization (F10 — tenth action: postmig-optimize)

The executor pulls **post-migration cloud metrics** (Cloud Monitor / Prometheus) and returns right_size / reserved / anomaly / perf recommendations for finalized hosts. Pre-mig `match.right_size` sizes a target from on-prem utilization; this is the post-mig counterpart: resize the now-running cloud resource from its actual cloud metrics.

`POST /v1/postmig-optimize`, `context` = target spec + a metrics window (e.g. `{"target": {...}, "metrics": {"cpu_p95": 8, "mem_p95": 22, "uptime_pct": 99.5}}`). Pushes a `PostMigRecommendation` back to `PUT /api/postmig-recs/{server_id}/{kind}` (one per kind, composite key `(server_id, kind)`). The `monthly_saving_usd` of right_size/reserved is rolled into the TCO by `cost.postmig_savings`.

`PostMigRecommendation` fields (see `idc/core/models.py`):

| Field | Meaning |
|---|---|
| `server_id` | Host stable identity (hostname lowercased), same convention as the DB profile. |
| `kind` | `PM_KINDS = {right_size, reserved, anomaly, perf}`; at most one rec per host per kind. |
| `from_spec` / `to_spec` | right_size: `from_spec`=current spec, `to_spec`=recommended spec; reserved: `to_spec`=reservation term (e.g. `1yr`); anomaly/perf leave these empty. |
| `reason` | Grounded in measured metrics (e.g. `p95 cpu 8% over 30d — under-utilized`). |
| `monthly_saving_usd` | Monthly saving (USD); anomaly/perf have no direct saving → `0.0`. Only right_size/reserved are non-zero and get rolled into cost. |
| `confidence` | 0..1. |
| `severity` | `low` / `medium` / `high` (anomaly urgency, etc.). |
| `detail` | Extra context (the anomaly's metric + time, the perf tuning point). |
| `scan_id` / `scanned_at` / `summary` / `updated_at` | Same as other profiles. |

### 1.11 Automated testing (F11 — eleventh, twelfth & thirteenth actions: test-gen / test-run / test-compare)

The executor generates test cases from the app's `CodeProfile`, runs them against a target (pre = on-prem, post = cloud), and diffs pre vs post. A regression blocks the wave's `finalize` via the **test_regression validation gate** — the real version of the old manual `process_up` stub.

- `POST /v1/test-gen` → pushes a TestCase list back to `PUT /api/test-cases/{app_id}`.
- `POST /v1/test-run` (`phase=pre|post`, `target`) → pushes a TestRun back to `PUT /api/test-runs/{run_id}`.
- `POST /v1/test-compare` → pushes a TestDiff back to `PUT /api/test-diffs/{app_id}`.

`TestDiff.regressions > 0` → the `test_regression` gate fails → `finalize` is blocked until the operator ack-skips or a re-run passes. With no diff, the gate is `pending` (which also blocks finalize, forcing a comparison first).

**Regression counting rule (authoritative)**: `regressions` = the count of `diff[]` items whose `verdict ∈ {regression, new_failure}`. `pass` and `flaky` **do not** count. Verdict definitions:

| verdict | Meaning | Counts as regression? |
|---|---|---|
| `pass` | pre and post both pass (or both fail identically) | no |
| `regression` | pre pass → post fail | yes |
| `new_failure` | pre skip/error → post fail (a failure newly introduced post-migration) | yes |
| `flaky` | inconsistent / pre-fail-but-post-pass needs human review | no (warning only, does not block) |

**Data-model fields** (see `idc/core/models.py`):

`TestCase` (`PUT /api/test-cases/{app_id}` pushes a `{"app_id", "cases": [...]}` list):

| Field | Meaning |
|---|---|
| `app_id` / `name` | Composite key (overwrites by app). `name` is unique within the app. |
| `kind` | `TEST_KINDS = {functional, integration, regression}`. |
| `endpoint` / `method` | HTTP URL or RPC target / method (default `GET`). |
| `request` | body/params/headers (dict). |
| `expected` | `{status, body_contains, ...}` (dict). |
| `setup` | setup/teardown note (fixtures, seed data). |
| `scan_id` / `updated_at` | Audit. |

`TestRun` (`PUT /api/test-runs/{run_id}` pushes one):

| Field | Meaning |
|---|---|
| `id` | `trun-...`. The `POST /v1/test-run` response includes `run_id` (idc-migrate may pre-mint it; if left empty, the executor mints it and returns it). |
| `app_id` / `phase` | `phase ∈ TEST_PHASES = {pre, post}` (pre = on-prem baseline before cutover, post = cloud after cutover). |
| `target` | The endpoint base actually hit (on-prem / cloud URL). |
| `results[]` | One `TestResult{case, status, duration_ms, response_summary, error}` per case. |
| `status` (per result) | `TS_* = {pass, fail, error, skip}`. |
| `passed` / `failed` / `errors` | Roll-up counts. |
| `run_at` / `scan_id` | Audit. |

`TestDiff` (`PUT /api/test-diffs/{app_id}` pushes one):

| Field | Meaning |
|---|---|
| `app_id` | Primary key (latest is taken). |
| `pre_run_id` / `post_run_id` | The two runs being compared. |
| `diff[]` | One `TestDiffItem{case, pre_status, post_status, verdict, detail}` per case. |
| `regressions` | Count of items with `verdict ∈ {regression, new_failure}` (**drives the test_regression gate**). |
| `scan_id` / `scanned_at` / `summary` / `updated_at` | Audit. |

---

## 2. Interface spec

**The executor exposes nothing to the network.** idc-migrate cannot reach it,
so it cannot push triggers to it (the old `POST {IDC_EXECUTOR_URL}/v1/scan`
request direction is gone, and idc-migrate no longer probes `/health`).
Everything flows one way — **executor → idc-migrate** — over two channels:

- **Pull (executor → idc-migrate)**: the executor asks idc-migrate for work via
  `POST /api/executor/tasks/claim`, gets a task (with the full work payload +
  the feedback `callback` baked in), and reports completion via
  `POST /api/executor/tasks/{id}/complete` (+ `/lease` heartbeats).
- **Push feedback (executor → idc-migrate)**: while/after working, the executor
  pushes results back over the `/api/*` feedback surface
  (`PUT /api/code-profiles/{app_id}`, `POST /api/change-jobs`, …) — unchanged.

idc-migrate is the server for both channels. Auth is **`Authorization: Bearer
<token>`** on every call; the token is the shared secret `IDC_EXECUTOR_TOKEN`
(configured per executor in the registry). The token also **identifies** the
executor: idc-migrate resolves it to an executor id, which gates routing (a
task may target one executor or the pool) and records `claimed_by`.

Content type `application/json; charset=utf-8`. Timestamps are ISO-8601 UTC (`...Z`).

### 2.0 Executor operational requirements (repo access + callback base)

Before implementing any business logic, the executor must meet two operational prerequisites — they aren't in the §1 action contract, but idc-migrate and the executor **interworking at all** depends on them:

**① Liveness (no `/health` probe — pull mode)**

idc-migrate **cannot reach the executor** (it exposes nothing), so there is no
`GET /health` probe and no `IDC_EXECUTOR_URL` round-trip. `executor_status`
reports pull mode (`reachable=null`); real liveness is "an executor is claiming
tasks", which idc-migrate infers from claim/heartbeat activity on the queue
(reported by `/api/executor/status`), not from a network probe. The executor
proves it is alive simply by polling `/api/executor/tasks/claim`. (An executor
may still implement `/health` for its own operator use; idc-migrate ignores it.)

**② Repo-access contract (prerequisite for scan/comb/modify)**

The `repo_url` in a scan/comb/modify task payload is **fetched and cloned by the executor itself** — idc-migrate does not clone on its behalf, does not pass repo credentials, and does not pass SSH private keys. So the executor's deployer must guarantee access to the target repo:

- Supported protocols: `git@host:org/repo.git` (SSH) and `https://host/org/repo.git` (HTTPS).
- SSH repos: the deployer provisions git + an SSH key in the executor's runtime environment (added to the repo's deploy keys / the user's authorized_keys), and the key must reach **every repo that could be scanned**. idc-migrate never ships private keys.
- HTTPS repos: the executor brings its own git credentials (credential helper / `~/.git-credentials` / env vars), provisioned by the deployer.
- Network reachability: the repo host (Gitlab/Gitee/GitHub) must be reachable from the executor's network (same VPC / VPN / public internet). An unreachable repo is the executor's error — mark the task `error` (via `/complete`) + push `ChangeJob.status=error` + an `error` message, **do not swallow it silently**.
- `branch` empty → the executor checks out the default branch.
- `modify` `mode=execute`: the executor edits on the **cloned working copy**, commits to a **new branch**, and pushes back (or opens a PR); `patch_ref` = branch name / commit sha / PR URL (executor-defined, but must be unique and traceable). Do not push directly to the `branch` given in the payload.

**③ Callback base (the target of executor → idc-migrate push)**

The executor pushes `CodeProfile`/`ChangeJob`/`DBConversionProfile`/`Question`/... back to idc-migrate over the public internet and needs an **idc-migrate public base URL**. Two sources, **the per-task `callback` takes precedence**:

1. **per-task `callback`**: idc-migrate bakes a `callback` into each task's payload (e.g. `https://mig.example.com/api/code-profiles/{app_id}`). This is **authoritative** — on completion the executor pushes to **this** URL (note: `callback` already includes the full path, so PUT/POST to it directly; do not re-build base + path yourself). idc-migrate builds it from `IDC_PUBLIC_URL` (see §2.6); when `IDC_PUBLIC_URL` is unset the field is empty and the executor falls back to (2).
2. **Fallback base** when `callback` is empty: an executor-side env var (the mock uses `IDC_MOCK_CALLBACK`; a **production executor should use the same name or `IDC_CALLBACK_BASE`**), set to idc-migrate's HTTPS public domain (e.g. `https://mig.example.com`). In this case the executor builds `PUT {base}/api/code-profiles/{app_id}` itself. **This is always required for the `change-jobs` heartbeat** (the heartbeat has no per-task callback, since it isn't a profile push) and for the multi-push / unknown-id actions (`postmig-optimize` pushes one rec per `{kind}`; `test-run` pushes to a `run_id` minted by idc-migrate) — both of which carry an empty `callback`.

Push requests always carry `Authorization: Bearer <IDC_EXECUTOR_TOKEN>` (the same secret as idc-migrate's side). The live idc-migrate has the web login gate on, so all `/api/*` require auth — bearer is the only option the executor can use (it has no browser session). The push endpoints sit behind idc-migrate's public nginx HTTPS ingress and accept the bearer before the login gate — **no extra port or certificate needed**.

> idc-migrate's `IDC_EXECUTOR_URL` points at the executor (request direction); idc-migrate's `IDC_PUBLIC_URL` and the executor's `IDC_MOCK_CALLBACK` / `IDC_CALLBACK_BASE` point at idc-migrate (push direction). `IDC_EXECUTOR_URL` points one way, the other three point the other way — don't confuse them.

### 2.1 Push: executor → idc-migrate

Base = idc-migrate's web address, e.g. `https://idc.example.com`.

#### `PUT /api/code-profiles/{app_id}` — report/overwrite an app's code assessment

Body = `CodeProfile` (`app_id` taken from the path; the body's `app_id` must match).

```json
{
  "app_id": "orders-svc",
  "repo_url": "git@gitlab:trade/orders-svc.git",
  "branch": "master",
  "scan_id": "cjob-1a2b3c4d5e",
  "scanner": "idc-executor/0.3.0",
  "scanned_at": "2026-07-12T08:30:00Z",
  "language": "java",
  "runtime": "jdk8",
  "framework": "spring-boot-2.1",
  "cloud_readiness": 0.42,
  "migration_pattern": "replatform",
  "refactor_effort": "medium",
  "code_deps": ["user-svc", "inventory-svc"],
  "network_endpoints": ["10.0.4.20:3306", "redis01.dc1:6379"],
  "required_changes": [
    {"title": "Parameterize DB host", "category": "db_connection", "file": "src/main/resources/application.yml", "effort": "low", "description": "Replace hardcoded 10.0.4.20 with ${DB_HOST}"}
  ],
  "blockers": ["Quartz scheduler assumes host crontab (scheduled_job, blocker)"],
  "findings": [
    {"category": "db_connection", "severity": "high", "file": "src/main/resources/application.yml", "line": 12, "message": "hardcoded DB host", "evidence": "url: jdbc:mysql://10.0.4.20:3306/orders", "remediation": "use ${DB_HOST} from env"}
  ],
  "summary": "Spring Boot 2.1 on JDK8; rehost blocked by Quartz-on-crontab; needs DB host param + service-discovery swap."
}
```

Response: `200 {"app_id": "...", "updated": true}`. Auth failure `401`; schema error `400`.

Idempotent: upsert by `app_id`, overwriting the old profile.

#### `PUT /api/db-profiles/{db_server_id}` — report/overwrite a DB host's heterogeneous-conversion assessment (F5)

Body = `DBConversionProfile` (`db_server_id` taken from the path; the body's `db_server_id` must match). `db_server_id` is the DB host's **stable identity** (hostname lowercased), **not** the volatile `Server.id` — the executor takes the hostname from the estate context so the profile survives across rebuilds (server id changes every rebuild). Bearer auth same as code-profiles.

```json
{
  "db_server_id": "db-oracle-01",
  "source_engine": "oracle",
  "target_engine": "tdsql",
  "difficulty": "C",
  "est_man_days": 40.0,
  "review_objects": ["PKG_ORDERS", "PKG_BILLING", "TRG_AUDIT_LOG"],
  "blockers": ["PL/SQL packages with no MySQL equivalent (DBMS_LOCK, UTL_FILE)"],
  "reverse_replication": true,
  "auto_convert_pct": 0.62,
  "scan_id": "cjob-db1",
  "scanned_at": "2026-07-12T08:00:00Z",
  "summary": "Oracle → TDSQL: heavy PL/SQL, hard conversion, reverse replication recommended."
}
```

Response: `200 {"db_server_id": "...", "updated": true}`. `GET /api/db-profiles` (list) / `GET /api/db-profiles/{id}` (one) / `DELETE` (bearer) follow the same pattern as code-profiles.

#### `POST /api/change-jobs` — report/heartbeat an execution job

Body = `ChangeJob`. The executor calls this on status changes (pending→running→done/error). Upsert by `id`.

```json
{
  "id": "cjob-1a2b3c4d5e",
  "app_id": "orders-svc",
  "kind": "scan",
  "repo_url": "git@gitlab:trade/orders-svc.git",
  "branch": "master",
  "status": "done",
  "patch_ref": "",
  "summary": "scanned 1248 files, 12 findings",
  "error": "",
  "created_at": "2026-07-12T08:29:00Z",
  "finished_at": "2026-07-12T08:30:00Z"
}
```

Response: `200 {"id": "...", "status": "..."}`.

#### Read-only queries (executor or human; bearer is enough, no session needed)

> Auth note: these GETs are **not anonymously open**. The live deployment has the web login gate on, so all `/api/*` require auth globally — push endpoints need bearer, and these read-only GETs need bearer too (or a browser session). When the executor pulls context, send `Authorization: Bearer <IDC_EXECUTOR_TOKEN>`, the same secret as the push endpoints. "No write auth needed" only means "no write permission/session required", **not** "auth-free". Putting a bearer on a GET does not leak estate info to the public internet.

- `GET /api/code-profiles` — all profiles (with a `findings` summary)
- `GET /api/code-profiles/{app_id}` — one
- `GET /api/change-jobs` — recent jobs
- `GET /api/db-profiles` / `GET /api/db-profiles/{db_server_id}` — DB conversion assessments (F5)

### 2.2 Pull: executor → idc-migrate (the executor asks for work)

The executor exposes nothing, so it **pulls** work from idc-migrate. All calls
carry `Authorization: Bearer <IDC_EXECUTOR_TOKEN>`; the token identifies the
executor (resolved to an executor id) and gates routing.

#### `POST /api/executor/tasks/claim` — pull one task

```json
{}        // optional: {"lease_seconds": 600}
```

Response `200`:

```json
{"task_id": "tsk-...", "kind": "scan", "status": "claimed",
 "payload": { /* the full work body — see "task payloads" below, incl `callback` */ },
 "executor_id": null, "claimed_at": "...Z", "lease_until": "...Z"}
```

Or `{"task_id": null, "status": "idle"}` when nothing is pending. The claim is
**atomic** and holds a **lease** (default 15 min): idc-migrate requeues any task
whose lease expired *before* this claim, then hands the oldest pending task the
caller is eligible for. Eligibility:

- a task with `executor_id == null` (**pool**) is claimable by **any** registered executor;
- a task with `executor_id == "db-spec"` is claimable **only** by the executor whose token resolves to `db-spec`.

So an executor polling with its token only ever receives tasks it may work on.
One task per call — loop with a short sleep when idle (the mock polls every
`IDC_MOCK_POLL_INTERVAL` s).

#### `POST /api/executor/tasks/{task_id}/complete` — report terminal state

```json
{"status": "done", "summary": "scanned 1248 files, 12 findings",
 "result_ref": "mock-patch-...", "error": ""}
```

`status ∈ {done, error}`. Only the **owning** executor (the one that claimed
it) may complete a task; a task whose lease already expired and was reclaimed
returns `409` (the work was taken over — treat as a no-op). On `done`/`error`
idc-migrate also mirrors the state into the `change-jobs` audit trail, so
existing ChangeJob consumers keep working.

#### `POST /api/executor/tasks/{task_id}/lease` — heartbeat

```json
{}        // optional: {"lease_seconds": 600}
```

Extends the claim's lease while still working (long jobs must renew before the
lease expires, or the task is requeued and another executor may reclaim it).
Only the owner may renew; a stale claim returns `409`.

#### `GET /api/executor/tasks/{task_id}` / `GET /api/executor/tasks` — status

The task row is the authoritative status (`pending|claimed|running|done|error`).
`GET /api/executor/tasks` lists the queue (newest-first; optional `status` /
`executor_id` filters) — operator/UI view.

#### Task payloads (the `payload` field the executor pulls)

The payload is exactly the body the old push-trigger used to send, now baked
into the task. Each kind carries its inputs + the feedback `callback`:

| kind | payload | feedback push |
|---|---|---|
| `scan` / `comb` | `{app_id, repo_url, branch, callback}` | `POST /api/change-jobs` then `PUT /api/code-profiles/{app_id}` |
| `modify` | `{app_id, repo_url, branch, mode, scope, changes[], notes[], callback:""}` | `POST /api/change-jobs` (heartbeat; uses IDC_CALLBACK_BASE) |
| `db-scan` | `{db_server_id, source_engine, target_engine, mode, callback}` | `POST /api/change-jobs` then `PUT /api/db-profiles/{db_server_id}` |
| `runtime-containerize` | `{app_id, server_id, inventory, mode, callback}` | `POST /api/change-jobs` then `PUT /api/code-profiles/{app_id}` (`source=runtime-derived`) |
| `legacy-disposition` | `{server_id, context, callback}` | `POST /api/change-jobs` then `PUT /api/legacy-dispositions/{server_id}` |
| `cutover-playbook` / `as-built` | `{wave_id, context, callback}` | `POST /api/change-jobs` then `PUT /api/docs/{slug}/{wave_id}` |
| `iac-emit` | `{scope, scope_id, context, callback}` | `POST /api/change-jobs` then `PUT /api/iac-artifacts/{scope_id}` |
| `postmig-optimize` | `{server_id, context, callback:""}` | `POST /api/change-jobs` then `PUT /api/postmig-recs/{server_id}/{kind}` ×N (uses IDC_CALLBACK_BASE) |
| `test-gen` | `{app_id, context, callback}` | `POST /api/change-jobs` then `PUT /api/test-cases/{app_id}` |
| `test-run` | `{app_id, phase, target, context, run_id, callback:""}` | `POST /api/change-jobs` then `PUT /api/test-runs/{run_id}` (uses IDC_CALLBACK_BASE) |
| `test-compare` | `{app_id, pre_run_id, post_run_id, callback}` | `POST /api/change-jobs` then `PUT /api/test-diffs/{app_id}` |

`db-scan` `mode`: `assess` (grade-only) | `convert` (also emit converted DDL +
per-object compatibility report in `DBConversionProfile.conversion`, F6). The
count of `blocked` objects drives the cutover gate. `modify` `mode`: `plan`
(dry-run, `patch_ref` only) | `execute` (writes + pushes a branch/PR); a
`runtime-derived` profile in `execute` is gated by an operator confirm
`Question` raised at enqueue time (see §1.8). `test-run` `run_id` is
**pre-minted by idc-migrate** and baked into the payload (the executor pushes
back under it; if empty the executor mints its own).

`modify` `changes[]` (core, built by idc-migrate): one item per change telling
the executor **which file/line, which literal (`old`) → which value (`new`)** —
assembled from the app's `CodeProfile` + operator `overrides`. The executor
locates-and-replaces directly; **no re-scan, no guessing**. When a `changes[]`
`old`/`new` is empty, idc-migrate couldn't extract the literal; the executor
must locate it by `file`+`line` or skip + mark the task `error` — **it must
never guess a value** (raise a Question instead, §2.3④).

Operator `overrides` (passed in the `POST /api/executor/trigger` body;
idc-migrate folds them into `changes`): an `old → new` map, e.g.
`{"10.0.4.20": "${ORDERS_DB_HOST}", "hunter2": "${ORDERS_DB_PASSWORD}"}`.
Matching a `changes` item's `old` overrides its `new`; unmatched ones are
reported in `notes`.

### 2.3 Executor ↔ idc-migrate continuous interaction

The executor is not fire-and-forget — while working it needs to interact with idc-migrate **repeatedly**, not just a one-shot trigger + terminal webhook. Four interaction channels:

**① Pull context (executor → idc-migrate, read-only GET; bearer is enough, no session)**

> Same as §2.1: under the live gate these GETs **still need bearer** (the same `IDC_EXECUTOR_TOKEN` as push), they are not anonymously open. When the executor pulls context, send `Authorization: Bearer <IDC_EXECUTOR_TOKEN>`.

When modifying code, the executor needs to know this app's dependency graph and what cloud target its DB server matched, so it can parameterize correctly. `changes` is a snapshot taken at trigger time, but the executor may need more context:

- `GET /api/workloads/{app_id}` → the app's `tier`, `depends_on`, `server_ids` (blast radius / change impact).
- `GET /api/apps/{app_id}/targets` → the migration targets matched for the app's servers, `{product, spec, region, confidence}`. E.g. a DB server matched to `CDB` (region=shanghai) → the executor knows the JDBC string should be parameterized to a CDB-style host, not a bare IP.
- `GET /api/code-profiles/{app_id}` → the app's current profile (the existing read-only port, restated here).

**② Ask "what should this value become" (executor → idc-migrate, read-only GET)**

`GET /api/apps/{app_id}/resolve?kind=ip&old=10.0.5.99`

When the executor scans a new hardcoded value **not** in the profile / `changes`, it must not guess — come back and ask idc-migrate. Resolution order (most trusted first):

1. Operator override (`old → new`) → `source="override", confidence=1.0`;
2. The **placeholder form** derived from the app's matched target (e.g. CDB → `${DB_HOST}`) → `source="match-derived", confidence≈0.4`;
3. A generic placeholder by kind → `source="default", confidence≈0.2`;
4. None apply → `new=null, source="unknown"`.

Response: `{"app_id": "...", "old": "10.0.5.99", "new": "${DB_HOST}", "source": "match-derived", "confidence": 0.4}`.

> Honesty rule: idc-migrate **never invents a concrete new endpoint** (a new CDB hostname is a human value that only exists at cutover time). It can only give the "form" (a placeholder like `${DB_HOST}`); `confidence<1` means "suggestion, needs operator confirmation". When `source="unknown"`, the executor **must** go back and ask the operator; it must not substitute on its own.

**③ Progress heartbeat (executor → idc-migrate, reuses `POST /api/change-jobs`)**

During a long job, while `status=running`, the executor repeatedly pushes `POST /api/change-jobs`, carrying human-readable progress in the `summary` field:

```json
{"id": "cjob-...", "app_id": "orders-svc", "kind": "scan",
 "status": "running", "summary": "scanned 600/1248 files, 8 findings so far", ...}
```

Each upsert overwrites `summary`, so the UI can show live progress. The terminal `status=done/error` goes through the same port (first `change-jobs` to mark done, then comb/scan does `PUT /api/code-profiles/{app_id}`). **No new fields** — reuses the existing `summary`, no schema change.

**④ Question/answer (executor → operator, relayed by idc-migrate)**

When the executor hits a `resolve` returning `unknown`, or a `changes` item with empty `old`/`new`, or scans a new hardcoded value not in the profile — **it does not guess and does not silently stall**; it raises a question, the operator answers in the UI, and the executor takes the answer and continues. This turns "idc-migrate doesn't know" into a blocking question rather than a blind substitution.

- **Executor raises** (bearer auth): `POST /api/apps/{app_id}/questions`

  ```json
  {"job_id": "cjob-...", "kind": "choice",
   "prompt": "What should '10.0.4.20:3306' in src/.../application.yml:12 (db_connection) be replaced with?",
   "options": ["${DB_HOST}:3306"],
   "context": {"file": "src/main/resources/application.yml", "line": 12,
               "category": "db_connection", "old": "10.0.4.20:3306", "new": ""}}
  ```

  `kind` ∈ `value` (operator fills a free value) / `choice` (pick from `options`) / `confirm` (yes/no). Returns `{"id": "qst-...", "status": "pending"}`.

- **Executor polls the answer**: `GET /api/questions/{id}` until `status=answered`, then reads `answer` and continues. (A "call-back-the-executor webhook" may be added later; polling is the default now, consistent with the async model.)
- **Operator answers** (no bearer, UI side): `POST /api/questions/{id}/answer`, body `{answer, answered_by?}` → sets `status=answered`; `POST /api/questions/{id}/skip` → `status=skipped` (the executor skips that change).
- **UI lists pending**: `GET /api/apps/{app_id}/questions?status=pending` (per app); `GET /api/questions?status=pending` (global, used by the Code-tab pending-queue panel).

idc-migrate provides a pure function `codeintel.question_for_change(app_id, change_item, job_id)`: it turns an **unresolved change item** produced by `build_change_spec` (empty `old` or `new`) into a `Question` draft the executor can POST directly. Already-resolved items return `None` (no question needed). This makes the whole chain closed-loop:

```
scan/comb → CodeProfile → build_change_spec → changes[]
                                          └─ unresolved items → question_for_change → Question
                                              └─ executor POST → operator answers → executor polls for the answer → real edit
```

### 2.4 Lifecycle and concurrency

- Triggers are async: a trigger immediately returns `200 + {task_id, status:"pending"}` (`job_id` is an alias for `task_id`). The task sits in the queue until an executor claims it.
- Lifecycle: `pending → claimed → (running, via heartbeat) → done|error`. An executor claims via `POST /api/executor/tasks/claim`, heartbeats via `/lease` while working, and marks terminal via `/complete`. A task whose lease expires is **requeued** to `pending` (a dead executor's work is reclaimable by another).
- Feedback order on completion: first `POST /api/change-jobs` (status=done), then the profile/artifact PUT (only for kinds that produce one). When idc-migrate receives a profile it sets the "rebuild-available" flag. idc-migrate also mirrors the terminal task state into `change-jobs` on `/complete`.
- Concurrency: a new scan for the same `app_id` overwrites the old profile; multiple executors may poll the same queue — the atomic claim guarantees each task is handed to exactly one.
- Error codes: `400` schema, `401` auth/unknown token, `404` unknown task/executor, `409` not-owned (complete/lease on a task the caller doesn't own — expired/requeued), `422` repo unreachable (reported via task `error`).

### 2.5 How idc-migrate uses this data ("extra reference signal")

After receiving a `CodeProfile`, the next `rebuild`:

1. **reconsolidate**: merges `code_deps` into `Workload.depends_on` (deduped); code-measured dependencies complete the edges missing from the static app graph.
2. **wave planning**: `plan_waves`, with the profiles —
   - adds edges to the app DAG from `code_deps` (affects topo order and wave dependencies);
   - within a topo layer, secondarily sorts by `refactor_effort` (high later) and `migration_pattern` (rewrite/refactor later);
   - the wave rationale carries the cloud_readiness and a blocker summary.
3. **migration strategy / match**: when `cloud_readiness < 0.5` or there's a `blocker`, lowers match.confidence and writes the blocker into the rationale; an app with `migration_pattern=rewrite` is flagged "not recommended for direct migration" in the strategy.

Implementation in `idc/core/codeintel.py` and `idc/core/__init__.py::rebuild`.

### 2.6 Connection and env vars (deployment checklist)

Each side configures itself; the only shared value is `IDC_EXECUTOR_TOKEN` (the bearer secret; must match on both sides).

**idc-migrate side** (see `idc/config.py` + `.env.example`):

| Var | Meaning |
|---|---|
| `IDC_EXECUTOR_TOKEN` | Shared bearer secret; validates the executor's pull + push calls, and **identifies** the executor (resolved to an executor id for routing). |
| `IDC_EXECUTOR_ENABLED` | `false` rejects trigger/enqueue requests (default `true`). |
| `IDC_PUBLIC_URL` | **This server's own public HTTPS base** (e.g. `https://mig.zaymuc.com`). Baked as the per-task `callback` so the executor can push results back here (see §2.0③). Empty (default) → `callback` is empty and the executor must fall back to its own `IDC_CALLBACK_BASE`; set this so the round-trip is self-contained from the task. |
| `IDC_EXECUTOR_URL` / `IDC_EXECUTOR_TIMEOUT` | **Unused for triggering in pull mode** (idc-migrate no longer reaches out). Kept in the config shape + Manage-executor panel for back-compat display only. |

The executor's TOKEN/`IDC_PUBLIC_URL` can also be written to the DB `system_config` at runtime via the `/executor` web panel (overrides env, no restart).

**executor side**:

| Var | Meaning |
|---|---|
| `IDC_EXECUTOR_TOKEN` | Same as above; must match the idc-migrate side. Identifies the executor on claim. |
| Callback base (`IDC_MOCK_CALLBACK` / production `IDC_CALLBACK_BASE`) | idc-migrate's public web address; the executor pulls `/api/executor/tasks/claim` from it and pushes `CodeProfile`/`ChangeJob` back here (the fallback when the per-task `callback` is empty, see §2.0③). **Always required** for the `change-jobs` heartbeat and the multi-push / unknown-id actions (`postmig-optimize`, `test-run`). In production this is idc-migrate's HTTPS public domain (e.g. `https://mig.example.com`). |
| Repo-access credentials (SSH key / git credentials) | Provisioned by the executor itself, ensuring it can clone/push `repo_url`. idc-migrate never ships them (see §2.0②). |
| Executor's own LLM | The executor **brings its own** LLM for semantic judgment (MigraQ). See §2.7. |

The pull + push endpoints (`/api/executor/tasks/*`, `/api/code-profiles`, `/api/change-jobs`, `/api/db-profiles`, `/api/legacy-dispositions`, `/api/iac-artifacts`, `/api/postmig-recs`, `/api/test-cases`, `/api/test-runs`, `/api/test-diffs`, `/api/docs`, `/api/apps/{app_id}/questions`) sit behind idc-migrate's public HTTPS ingress, before bearer auth — **no extra port or certificate needed**, all reachable from the internet over HTTPS with the bearer token.

### 2.6.1 Multiple executors

idc-migrate can drive **more than one executor**. The `default` executor is the one configured above (env `IDC_EXECUTOR_TOKEN` / the Manage-executor panel). Additional **named** executors are registered via the registry API:

- `GET /api/executors` — list all (default first; token never returned, only `token_set`).
- `PUT /api/executors/{id}` — upsert a named executor `{url?, token?, enabled, timeout}` (id != `default`; the default is managed via `/api/executor/config`). `url` is unused in pull mode but kept for back-compat.
- `DELETE /api/executors/{id}` — remove a named executor (the default can't be deleted).
- `POST /api/executors/{id}/test` — report pull-mode status for a candidate config without persisting.

Every trigger request body takes an optional **`executor_id`**: empty/`""` → the **pool** (any registered executor may claim it); `"default"` or a named id → **target** that executor specifically (`404` if the named id isn't registered). The web Trigger cards expose a per-card executor picker; the CLI exposes `--executor`; the bare API takes `executor_id` in the JSON body.

Pull routing is by token: an executor's `/api/executor/tasks/claim` is resolved to its executor id, and it only receives tasks where `executor_id IS NULL` (pool) or `executor_id == <its id>`. Every executor pushes feedback to the **same** idc-migrate public URL (`IDC_PUBLIC_URL`, global) and authenticates with **its own** token; idc-migrate accepts a bearer that matches **any** registered executor's token (the default's included), so pushes from any configured executor validate.

### 2.7 LLM boundary (executor brings its own; idc-migrate does not share)

> To be explicit: **the executor uses its own LLM for semantic judgment; idc-migrate neither exposes nor shares its LLM with the executor.**

- idc-migrate's own MigraQ uses a local Ollama gateway (`IDC_LLM_BASE` / `ANTHROPIC_BASE_URL`, default `http://127.0.0.1:11434`, model `glm-5.2:cloud`). That endpoint **listens only on `127.0.0.1` and is not exposed publicly** — an executor connecting from the internet couldn't reach it even if it tried.
- The only thing crossing the wire is JSON (`CodeProfile`/`ChangeJob`/`changes`) + the bearer secret; **the contract contains no LLM call**. Which LLM/model the executor picks is its deployer's business; idc-migrate neither needs nor should know.
- So the executor side must configure its own LLM backend (endpoint + model + credentials); the variable names are chosen by the executor. There is no and no need for a corresponding `IDC_LLM_BACKEND`-style variable on the idc-migrate side.