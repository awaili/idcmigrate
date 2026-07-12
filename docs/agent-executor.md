# 外接智能体执行机 — 接口规范与要求

idc-migrate 把"代码扫描 / 梳理 / 修改"外包给一台**外接智能体执行机**（以下简称 *executor*）。executor 扫描应用源码仓库、产出迁移评估、并按需改代码；评估结果回推给 idc-migrate，作为 **reconsolidate、wave planning、迁移策略** 的额外参考信号。

本文定义两件事：

1. **对 executor 的要求** —— 它必须能做什么、扫描分类、产出义务、质量底线。
2. **接口规范** —— 两个方向的 REST 契约、schema、鉴权、生命周期、webhook。

> 数据模型见 `idc/core/models.py`：`CodeProfile`（按 app 聚合的代码评估）、`ScanFinding`（单条发现）、`ChangeJob`（每次 scan/comb/modify 的审计记录）。

---

## 1. 对 executor 的要求

### 1.1 三类动作

| 动作 | 输入 | 产出 |
|---|---|---|
| **scan（扫描）** | `app_id`、`repo_url`、`branch`（可选） | 一组 `ScanFinding`（按 §1.2 分类） |
| **comb（梳理）** | scan 的发现 + 仓库 | 一个 `CodeProfile`：cloud_readiness、migration_pattern、refactor_effort、code_deps、required_changes、blockers、summary |
| **modify（修改）** | `changes`（idc-migrate 从 CodeProfile + 操作员 overrides 拼出的具体改清单）+ 仓库 + 授权 | 实际改代码：参数化 IP、替换服务发现、容器化、抽配置…；产出 `patch_ref`（commit/PR/artifact） |

scan 是只读；comb 是只读分析；**modify 才有副作用**，必须显式授权（`mode=execute`），且默认 dry-run。

### 1.2 扫描分类（必须覆盖，输出用这些 `category`）

`hardcoded_ip` · `service_discovery` · `db_connection` · `stateful_local` · `scheduled_job` · `secrets_in_repo` · `baremetal_assumption` · `network_dependency` · `os_dependency` · `legacy_runtime` · `config_coupling`

**F5 — DB schema/SQL 转换分类**（异构 DB 评估时使用，见 §1.5）：`plsql_compat`（Oracle PL/SQL 无 MySQL 等价物的构造）· `db_feature_gap`（引擎特有功能差距：序列、分区、物化视图…）· `db_size_complexity`（schema 规模/对象数驱动转换成本）。

每条 `ScanFinding` 字段：`category`、`severity`(`low|medium|high|blocker`)、`file`(仓库相对路径)、`line`、`message`、`evidence`(截断的代码片段/匹配 token)、`remediation`。

### 1.3 产出义务（CodeProfile 必填项）

- `cloud_readiness` ∈ [0,1]：综合 findings 的 severity 与数量给出。`blocker` 级 finding 必须把 readiness 压到 < 0.5。
- `migration_pattern` ∈ {`rehost`,`replatform`,`refactor`,`rewrite`}：有 blocker 且不可改 → `rewrite`；需改连接/服务发现但骨架可用 → `replatform`；中等改动 → `refactor`；纯搬迁 → `rehost`。
- `refactor_effort` ∈ {`low`,`medium`,`high`}：按 required_changes 的数量与风险估。
- `code_deps`：从代码里**实测**到的对其它 `app_id` 的依赖（HTTP/RPC/消息/共享库调用），用于补全应用依赖图。这是 reconsolidate/waveplan 最关键的增量信号。
- `blockers`：阻止直接迁移的硬阻塞（人话描述，会进 match rationale）。
- `required_changes`：每条 `{title, category, file, effort, description}`。
- `scanned_at` / `scanner`（执行机名+版本）/ `scan_id`（产出该 profile 的 job id）。

### 1.4 质量底线

- **可追溯**：每条 finding 必须带 `file`+`line`+`evidence`；不得给无定位的泛泛结论。
- **确定性优先**：规则类检查必须稳定可复现；只有需要语义判断的项才用 LLM，且 LLM 结论必须落到带证据的 finding 上。
- **幂等**：同一 `(app_id, repo_url, branch)` 重跑，`CodeProfile` 是 upsert（按 `app_id` 覆盖），不产生重复。
- **截断**：`evidence` ≤ 512 字符；单 app 的 `findings` 建议上限 500 条，超出按 severity 取 top。
- **失败可观测**：执行出错必须回传 `ChangeJob.status=error` + `error` 文案，不得静默吞掉。
- **默认只读**：`modify` 默认 dry-run（只产 `patch_ref` 不落库）；真正写仓库需要调用方传 `mode=execute`。

### 1.5 DB 异构转换评估（F5 — 第四类动作 db-scan）

针对**数据库主机**（`Server.role == "db"` 且源引擎是 Oracle/SQLServer 的主机），executor 增加一个 `db-scan` 动作：扫 DB schema/SQL，产出 `DBConversionProfile`（见 `idc/core/models.py`）。它与 `CodeProfile` 并列，但按**DB 主机的稳定标识**（hostname，不是易变的 `Server.id`——每次 rebuild id 都会变）聚合。

`DBConversionProfile` 字段：

- `db_server_id`：DB 主机的稳定标识（hostname 小写）。**这是主键**，executor 上报时用它。
- `source_engine` / `target_engine`：`oracle` / `sqlserver` / `mysql` → `tdsql` / `cdb_mysql`。
- `difficulty` ∈ {`A`,`B`,`C`}：转换难度等级（对标 Ora2Pg A–C / AWS DMS SC 质量分）。`A`=几乎全自动可转、`B`=少量对象需人工评审、`C`=重 PL/SQL / 引擎特有功能、需 replatform。
- `est_man_days`：人工转换工日估算。
- `review_objects`：需人工评审的对象列表（包/触发器/视图）。
- `blockers`：硬阻塞（人话，进 match rationale）。
- `reverse_replication`：是否在 cutover 窗口保留反向复制通道（回滚安全网；F6 db-kind 任务在 `finalized` 前保留此通道）。
- `auto_convert_pct` ∈ [0,1]：规则引擎可自动转换的比例（对标 DMS SC）。
- `scan_id` / `scanned_at` / `summary`。

**idc-migrate 如何使用**（下一次 `rebuild`）：`codeintel.enrich_match_db` 按 difficulty 调整该 DB 主机的 `Match`——`C` 级强降 confidence + 强制 `replatform`（直接 rehost 会带上未转换的 PL/SQL）+ 把 blockers 写进 rationale；`B` 级小幅降 confidence；`A` 级不降，仅标注已评估。`wave_risk_basis` 把含 `C` 级 DB 主机的 wave 风险上调。`reverse_replication=True` 在 F6 db-kind cutover 的回滚路径里保留通道直到 `finalized`。

### 1.6 无源码容器化（F9 — 第五类动作 runtime-containerize）

针对**没有源码仓库**的遗留 app（厂商打包件 / 纯二进制），executor 增加一个 `runtime-containerize` 动作：从 Zabbix/Prometheus 发现的**运行时清单**（进程 + 端口 + 软件清单）推断一个 Dockerfile 脚手架，产出一个 `CodeProfile`（同 §1.3 结构），但 `source` 字段标为 `runtime-derived`（而非默认的 `repo`）。

字段约束：
- `source ∈ {"repo", "runtime-derived"}`：profile 的来源。`runtime-derived` 表示从运行时遥测推断（无源码），confidence 被 cap 在 0.5，且 `modify` 在 `execute` 模式下必须先过操作员确认（一个 `Question`，见下）。
- `required_changes` 里的 Dockerfile 脚手架变更 `file="Dockerfile"`，`patch_ref` 指向推断出的脚手架。
- `findings` 记录推断证据（`process=`/`port=`/`unit=`）。

**确认门（confirm gate）**：`runtime-derived` profile 是脚手架*推断*，不是扫描。在 `modify` `mode=execute` 写入前，idc-migrate 抬一个 `Question`（`kind=choice`，选项 `confirmed — proceed` / `reject — needs manual review`）；操作员回答后才能再触发 `modify`。`mode=plan`（dry-run 预览脚手架）不受门限制。

---

## 2. 接口规范

两个方向：

- **Push（executor → idc-migrate）**：executor 把 CodeProfile / ChangeJob 推进来。idc-migrate 是服务端。
- **Request（idc-migrate → executor）**：idc-migrate 请求 executor 跑 scan/comb/modify。executor 是服务端。

两边都用 **`Authorization: Bearer <token>`** 共享密钥鉴权。idc-migrate 侧密钥 = `IDC_EXECUTOR_TOKEN`（校验 push）；executor 侧密钥由部署方配置（idc-migrate 用同一 `IDC_EXECUTOR_TOKEN` 发起 request）。

内容类型 `application/json; charset=utf-8`。时间戳统一 ISO-8601 UTC（`...Z`）。

### 2.1 Push：executor → idc-migrate

基址 = idc-migrate 的 web 地址，如 `https://idc.example.com`。

#### `PUT /api/code-profiles/{app_id}` — 上报/覆盖一个 app 的代码评估

请求体 = `CodeProfile`（`app_id` 取路径值，体里的 `app_id` 必须一致）。

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

响应：`200 {"app_id": "...", "updated": true}`。鉴权失败 `401`；schema 错 `400`。

幂等：按 `app_id` upsert，覆盖旧 profile。

#### `PUT /api/db-profiles/{db_server_id}` — 上报/覆盖一个 DB 主机的异构转换评估（F5）

请求体 = `DBConversionProfile`（`db_server_id` 取路径值，体里的 `db_server_id` 必须一致）。`db_server_id` 是 DB 主机的**稳定标识**（hostname 小写），**不是**易变的 `Server.id`——executor 从 estate 上下文里拿 hostname 上报，这样 profile 能跨 rebuild 存活（server id 每次 rebuild 都变）。Bearer 鉴权同 code-profiles。

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

响应：`200 {"db_server_id": "...", "updated": true}`。`GET /api/db-profiles`（列表）/ `GET /api/db-profiles/{id}`（单条）/ `DELETE`（bearer）同 code-profiles 模式。

#### `POST /api/change-jobs` — 上报/心跳一个执行任务

请求体 = `ChangeJob`。executor 在任务状态变化时调用（pending→running→done/error）。按 `id` upsert。

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

响应：`200 {"id": "...", "status": "..."}`。

#### 只读查询（executor 或人查，无需写鉴权）

- `GET /api/code-profiles` — 全部 profile（带 `findings` 摘要）
- `GET /api/code-profiles/{app_id}` — 单个
- `GET /api/change-jobs` — 最近任务
- `GET /api/db-profiles` / `GET /api/db-profiles/{db_server_id}` — DB 转换评估（F5）

### 2.2 Request：idc-migrate → executor

基址 = `IDC_EXECUTOR_URL`。所有请求带 `Authorization: Bearer <IDC_EXECUTOR_TOKEN>`。

#### `POST /v1/scan` — 触发扫描

```json
{"app_id": "orders-svc", "repo_url": "git@gitlab:trade/orders-svc.git",
 "branch": "master", "callback": "https://idc.example.com/api/code-profiles/orders-svc"}
```

响应 `202`：`{"job_id": "cjob-...", "status": "pending"}`。完成后 executor **回调** push 接口（先 `POST /api/change-jobs` 置 done，再 `PUT /api/code-profiles/{app_id}`）。

#### `POST /v1/comb` — 触发梳理（产 CodeProfile）

同上 schema；`kind=comb`。

#### `POST /v1/db-scan` — 触发 DB 异构转换评估（F5）

```json
{"db_server_id": "db-oracle-01", "source_engine": "oracle",
 "target_engine": "tdsql", "callback": "https://idc.example.com/api/db-profiles/db-oracle-01"}
```

`db_server_id` 是 DB 主机的**稳定标识**（hostname），executor 从 estate 上下文拿到（idc-migrate 在触发时给出）。响应 `202`：`{"job_id": "...", "status": "pending"}`。完成后 executor **回调**：先 `POST /api/change-jobs` 置 done（`kind=db-scan`），再 `PUT /api/db-profiles/{db_server_id}` 推 `DBConversionProfile`。

#### `POST /v1/runtime-containerize` — 触发无源码容器化（F9）

```json
{"app_id": "legacy-billing", "server_id": "srv-123",
 "inventory": {"process": "java -jar billing.jar", "port": 8080,
               "unit": "billing.service", "software": ["openjdk-8"]},
 "mode": "plan", "callback": "https://idc.example.com/api/code-profiles/legacy-billing"}
```

无 `repo_url`——executor 从 `inventory`（Zabbix/Prometheus 发现的进程+端口+软件清单）推断 Dockerfile 脚手架。响应 `202`：`{"job_id": "...", "status": "pending"}`。完成后 executor **回调**：先 `POST /api/change-jobs` 置 done（`kind=runtime-containerize`，`patch_ref` 指向推断出的 Dockerfile 脚手架），再 `PUT /api/code-profiles/{app_id}` 推一个 `source=runtime-derived` 的 `CodeProfile`。`mode=plan` 只产脚手架预览；`mode=execute` 写入（但 idc-migrate 会在写入前抬一个确认 `Question`，见 §1.6）。

#### `POST /v1/modify` — 触发修改

```json
{"app_id": "orders-svc", "repo_url": "git@gitlab:trade/orders-svc.git",
 "branch": "cloud-ready", "mode": "plan", "scope": ["db_connection", "service_discovery"],
 "changes": [
   {"category": "db_connection", "kind": "endpoint",
    "file": "src/main/resources/application.yml", "line": 12,
    "title": "Parameterize DB host",
    "evidence": "url: jdbc:mysql://10.0.4.20:3306/orders",
    "old": "10.0.4.20:3306", "new": "${ORDERS_DB_HOST}:3306",
    "effort": "low", "description": "Replace hardcoded 10.0.4.20 with ${DB_HOST}",
    "note": "new value from operator override"},
   {"category": "secrets_in_repo", "kind": "secret",
    "file": "src/main/resources/application.yml", "line": 13,
    "title": "Move DB password to secret ref",
    "evidence": "password: hunter2",
    "old": "hunter2", "new": "${ORDERS_DB_PASSWORD}",
    "effort": "low", "description": "Inline password → secret ref",
    "note": "new value from operator override"}
 ],
 "notes": []}
```

`mode=plan`（默认）= dry-run，只产 `patch_ref` 不落仓库；`mode=execute` = 真改并推分支/PR。

- **`changes`（核心，idc-migrate 生成）**：具体改清单，每条一个变更——告诉执行机**改哪个文件哪一行、把哪个字面量（`old`）换成什么（`new`）**。由 idc-migrate 用该 app 的 `CodeProfile`（`required_changes` + `findings` 按 file+line join）拼出，再叠操作员 `overrides`。执行机据此直接定位替换，**不必重新扫仓库、不必猜改什么**。`scope` 仍作为 category 过滤保留（向后兼容），但 `changes` 已按 scope 过滤过，是权威指令。
- **`changes[].old` / `new` 留空时**：idc-migrate 没能从 evidence 抽出字面量（或密码被脱敏），`note` 说明原因；执行机须按 `file`+`line` 自己定位，或跳过该条并回 `ChangeJob.status=error` 报告。**不得自己猜一个值去替换。**
- **`notes`**：idc-migrate 给执行机/操作员的提示，例如"该 app 没有 CodeProfile，请先 scan"、"N 条 override 没匹配到 finding 已忽略"、"profile 里没有 in-scope 的 required_changes"。

操作员 `overrides`（在 `POST /api/executor/trigger` 请求体里传，idc-migrate 折进 `changes`）：一个 `old → new` 的 map，例如
`{"10.0.4.20": "${ORDERS_DB_HOST}", "hunter2": "${ORDERS_DB_PASSWORD}"}`。匹配到 `changes` 里某条的 `old` 就覆盖其 `new`；没匹配到的会在 `notes` 里报告。这让操作员把"迁移后的新值"喂给执行机，而 idc-migrate 不必自己猜新 CDB 主机名这种 cutover 期才有的人工值。

响应 `202`：`{"job_id": "cjob-...", "status": "pending"}`。

#### `GET /v1/jobs/{job_id}` — 查任务状态

响应 `200`：`ChangeJob`。

### 2.3 执行机 ↔ idc-migrate 的持续交互

执行机不是 fire-and-forget——它在干活期间需要**反复**跟 idc-migrate 交互，而不是只靠一次性 trigger + 终态 webhook。三条交互通道：

**① 拉上下文（执行机 → idc-migrate，只读 GET，无需写鉴权）**

执行机改代码时要知道这个 app 的依赖图、它的 DB 服务器匹配到了什么云目标，才能正确参数化。`changes` 是 trigger 时刻的快照，但执行机可能需要更多上下文：

- `GET /api/workloads/{app_id}` → 该 app 的 `tier`、`depends_on`、`server_ids`（blast radius / 改动影响面）。
- `GET /api/apps/{app_id}/targets` → 该 app 各服务器匹配到的迁移目标 `{product, spec, region, confidence}`。例：DB 服务器匹配到 `CDB`（region=shanghai）→ 执行机知道 JDBC 串应参数化成 CDB 风格主机，而非裸 IP。
- `GET /api/code-profiles/{app_id}` → 该 app 当前 profile（已有的只读口子，这里复述）。

**② 问"这个值改成什么"（执行机 → idc-migrate，只读 GET）**

`GET /api/apps/{app_id}/resolve?kind=ip&old=10.0.5.99`

执行机扫到 profile / `changes` 里**没有**的新硬编码值时，不要猜——回来问 idc-migrate。解析顺序（最可信在前）：

1. 操作员 override（`old → new`）→ `source="override", confidence=1.0`；
2. 由 app 匹配目标推导的**占位符形式**（如 CDB → `${DB_HOST}`）→ `source="match-derived", confidence≈0.4`；
3. 按 kind 的通用占位符 → `source="default", confidence≈0.2`；
4. 都不适用 → `new=null, source="unknown"`。

响应：`{"app_id": "...", "old": "10.0.5.99", "new": "${DB_HOST}", "source": "match-derived", "confidence": 0.4}`。

> 诚实规则：idc-migrate **永远不编一个具体的新端点**（新 CDB 主机名是 cutover 期才有的人工值）。只能给到"形式"（`${DB_HOST}` 这种占位符），`confidence<1` 即"建议，需操作员确认"。`source="unknown"` 时执行机**必须**回头问操作员，不得自行替换。

**③ 进度心跳（执行机 → idc-migrate，复用 `POST /api/change-jobs`）**

长任务期间，执行机在 `status=running` 时**反复**推 `POST /api/change-jobs`，用 `summary` 字段带人类可读进度：

```json
{"id": "cjob-...", "app_id": "orders-svc", "kind": "scan",
 "status": "running", "summary": "scanned 600/1248 files, 8 findings so far", ...}
```

每次 upsert 覆盖 `summary`，UI 即可显示实时进度。终态 `status=done/error` 同样走这个口子（先 `change-jobs` 置 done，comb/scan 再 `PUT /api/code-profiles/{app_id}`）。**无新字段**——复用现有 `summary`，不引入 schema 改动。

**④ 提问/回答（执行机 → 操作员，经 idc-migrate 中转）**

执行机遇到 `resolve` 回 `unknown`、或 `changes` 里某条 `old`/`new` 留空、或扫到 profile 没有的新硬编码值时——**不猜、不静默卡住**，而是发起一个问题，操作员在 UI 回答，执行机拿到答案继续。这把"idc-migrate 不知道"变成"阻塞式提问"而不是"瞎替换"。

- **执行机发起**（bearer 鉴权）：`POST /api/apps/{app_id}/questions`

  ```json
  {"job_id": "cjob-...", "kind": "choice",
   "prompt": "What should '10.0.4.20:3306' in src/.../application.yml:12 (db_connection) be replaced with?",
   "options": ["${DB_HOST}:3306"],
   "context": {"file": "src/main/resources/application.yml", "line": 12,
               "category": "db_connection", "old": "10.0.4.20:3306", "new": ""}}
  ```

  `kind` ∈ `value`（操作员填自由值）/ `choice`（从 `options` 选）/ `confirm`（是/否）。返回 `{"id": "qst-...", "status": "pending"}`。

- **执行机轮询答案**：`GET /api/questions/{id}` 直到 `status=answered`，读 `answer` 继续。（未来可加"答完回调执行机"webhook；现以轮询为默认，与异步模型一致。）
- **操作员回答**（无 bearer，UI 侧）：`POST /api/questions/{id}/answer`，体 `{answer, answered_by?}` → 置 `status=answered`；`POST /api/questions/{id}/skip` → `status=skipped`（执行机跳过该条改动）。
- **UI 列出待答**：`GET /api/apps/{app_id}/questions?status=pending`（按 app）；`GET /api/questions?status=pending`（全局，Code-tab 的待答队列面板用）。

idc-migrate 提供纯函数 `codeintel.question_for_change(app_id, change_item, job_id)`：把 `build_change_spec` 产出的**未解析 change 项**（`old` 或 `new` 为空）自动转成一个 `Question` 草稿，执行机直接 POST。已解析的项返回 `None`（无需问）。这样整条链是闭环的：

```
scan/comb → CodeProfile → build_change_spec → changes[]
                                          └─ 未解析项 → question_for_change → Question
                                              └─ 执行机 POST → 操作员答 → 执行机轮询拿答案 → 真改
```

### 2.4 生命周期与并发

- 任务异步：request 立即回 `202 + job_id`；结果靠 webhook 回调或轮询 `GET /v1/jobs/{id}`。
- 回调顺序：先 `change-jobs`（status=running/done），再 `code-profiles`（仅 comb/scan 产 profile 时）。idc-migrate 收到 profile 即触发"可重建"标记。
- 超时：idc-migrate 侧 `IDC_EXECUTOR_TIMEOUT`（默认 600s）只约束 request 的 HTTP 连接，不约束任务本身；任务长时由 `ChangeJob.status` 表达。
- 并发：同一 `app_id` 的新 scan 会覆盖旧 profile；同一 `job_id` 重传为 upsert。
- 错误码：`400` schema、`401` 鉴权、`404` app 未知、`409` 已有进行中任务（可选）、`422` 仓库不可达、`5xx` 执行机内部错。

### 2.5 idc-migrate 如何使用这些数据（"更多参考信息"）

收到 `CodeProfile` 后，下一次 `rebuild` 会：

1. **reconsolidate**：把 `code_deps` 合并进 `Workload.depends_on`（去重），代码实测的依赖补全静态 app 图里缺的边。
2. **wave planning**：`plan_waves` 拿到 profiles 后——
   - 用 `code_deps` 给应用 DAG 补边（影响拓扑序与 wave 依赖）；
   - 同拓扑层内按 `refactor_effort`（high 靠后）与 `migration_pattern`（rewrite/refactor 靠后）二次排序；
   - wave rationale 里带上 cloud_readiness 与 blocker 摘要。
3. **迁移策略 / match**：`cloud_readiness < 0.5` 或有 `blocker` 时下调 match.confidence 并把 blocker 写进 rationale；`migration_pattern=rewrite` 的 app 在策略里标注"不建议直接迁移"。

实现见 `idc/core/codeintel.py` 与 `idc/core/__init__.py::rebuild`。