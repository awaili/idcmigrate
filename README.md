# idc-migrate — IDC → Tencent Cloud migration copilot

A robust system to assist migrating an IDC (15K+ servers, 200K+ cores) to
Tencent Cloud. Accessible from **web** and **CLI**, with two AI layers:

- **MigraQ layer** — local gateway (`glm-5.2:cloud`) for match explanations,
  RAG Q&A over the inventory, wave briefs, right-sizing.
- **Agent layer** — the `claude` CLI as a subprocess for agentic tasks:
  generate Terraform, draft runbooks, verify SG equivalence, run pre-flight
  checks (plan-mode = read-only by default; `--execute` to allow side-effects).
- **External executor** — an out-of-process agent that **scans / combs /
  modifies application source** and pushes a per-app `CodeProfile` back here.
  Code-level deps, cloud-readiness, refactor effort and blockers then feed
  reconsolidate, wave planning and migration strategy. Interface spec:
  [`docs/agent-executor.md`](docs/agent-executor.md).

Inventory is ingested from **four sources**, normalized into one `Server`
model with per-field provenance + cross-source entity resolution:

| source | online | offline |
|---|---|---|
| ServiceNow CMDB | REST `cmdb_ci_server` (basic/token auth) | `fixtures/servicenow_cmdb_ci_server.csv` |
| RVTools | — (vSphere export file) | `fixtures/rvtools_v{Info,Disk,Network}.csv` |
| Zabbix | JSON-RPC `host.get` + `item/history.get` | `fixtures/zabbix_hosts.json` |
| Prometheus | PromQL `/api/v1/query` | `fixtures/prometheus_metrics.json` |

Every source has a fixture, so the system **runs out of the box** with no
credentials. Set env vars (see `.env.example`) to switch a source to live API.
You can also point any source's offline file at your own data via `IDC_*_PATH`,
or upload a file from the web UI (see **Uploading source files** below).

## Architecture

```
idc/
├── core/          # unified model + MariaDB store + 4 ingest adapters
│   ├── normalize.py   # cross-source entity resolution + provenance
│   ├── match.py       # rule engine → Tencent Cloud (CVM/TKE/CDB/Redis/EMR/…)
│   ├── codeintel.py   # turns executor CodeProfiles into wave/strategy signals
│   └── plan.py        # wave planning + dependency DAG (code-aware)
├── llm/           # Ollama-compatible gateway client (explain/ask/summarize/right-size)
├── agent/         # Claude Code CLI runner (stream-json, plan/execute modes)
├── backend/       # FastAPI: REST + WebSocket + static web
└── cli/           # `idc` CLI (typer/rich) — shares core with the backend
web/               # single-page UI (vanilla, no build)
fixtures/          # sample data for all 4 sources (15 servers, 7 apps)
tests/             # pytest (12 tests)
```

The CLI and web backend share the **same core library**, so behaviour is
identical. MariaDB stores raw assets, servers, matches, waves, workloads,
ingest runs, agent tasks, code profiles and executor change jobs (audit trail).

## Quick start

```bash
pip install -r requirements.txt        # fastapi uvicorn httpx pydantic typer rich

# CLI
python -m idc.cli.main ingest --source all
python -m idc.cli.main rebuild
python -m idc.cli.main inventory --role db
python -m idc.cli.main waves
python -m idc.cli.main ask "which servers run mysql and what do they map to?"
python -m idc.cli.main agent "draft a terraform module for the LZ VPC" --stream
python -m idc.cli.main code profiles               # code profiles from executor
python -m idc.cli.main code scan --app orders --repo git@gitlab:t/orders.git
python -m idc.cli.main doctor          # check config / MigraQ / claude

# Web
python -m idc.cli.main serve --port 8010   # → http://localhost:8010
```

Or use the launcher (creates a venv, installs deps, forwards args):
```bash
./run.sh ingest --source all
./run.sh serve --port 8010
```

## Matching → Tencent Cloud

Rule engine maps each server by role/source-type/OS/sizing:

| source | → product |
|---|---|
| mysql/mariadb DB | **CDB** TencentDB for MySQL (HA/RO, sized by RAM + data disk) |
| Oracle DB | **CVM** lift-and-shift (BYOL); alt TDSQL |
| Redis cache (high crit) | **TencentDB for Redis**; else CVM self-hosted |
| k8s master | **TKE** managed control plane |
| k8s worker | **CVM** (TKE node pool, right-sized SA5) |
| baremetal Hadoop | **EMR** cluster; alt CPM |
| web / app / monitoring | **CVM** (SA5, right-sized) + CBS data disk |

CVM sizing picks the smallest SA5 type that fits CPU+RAM (right-size, not
over-provision). The target landing zone is Thailand, so every datacenter
maps to Tencent Cloud's Bangkok region (`ap-bangkok`). Edit
`idc/core/match.py` (`REGION_MAP`) to fit the real env.

## Wave planning

```
1 Landing Zone   TKE, VPC/COS, observability       (no deps)
2 Data           CDB, Redis, Oracle CVM             (depends: 1)
3 Application    per-app waves, topologically       (depends: 2 + app deps)
4 Cutover        top-of-DAG frontends (traffic sw)  (depends: 2 + 3)
```

A server belongs to **exactly one** wave (role-based stage wins over app
grouping, so infra hosts aren't duplicated into their app's wave).

## Configuration

All settings are env-driven (`.env.example`):
- `IDC_LLM_BASE` / `IDC_LLM_MODEL` — MigraQ gateway (default local Ollama-fronted
  `glm-5.2:cloud`). `IDC_LLM_ENABLED=false` forces rule-only.
- `IDC_CLAUDE_BIN` / `IDC_CLAUDE_DEFAULT_MODE` — Claude Code agent.
- `IDC_EXECUTOR_URL` / `IDC_EXECUTOR_TOKEN` / `IDC_EXECUTOR_ENABLED` — external
  code executor (scan/comb/modify). Token is the shared bearer secret for both
  directions. See `docs/agent-executor.md`.
- `IDC_SERVICENOW_*`, `IDC_ZABBIX_*`, `IDC_PROM_URL`, `IDC_RVTOOLS_PATH` —
  per-source online config; empty → fixture.
- `IDC_SERVICENOW_PATH`, `IDC_ZABBIX_PATH`, `IDC_PROMETHEUS_PATH`,
  `IDC_RVTOOLS_PATH` — offline file path per source (defaults to the bundled
  fixture). Point these at your own CSV/JSON/xlsx to ingest real exports
  without live API creds.

## Uploading source files

The web UI has an **Upload file…** button (next to Ingest/Rebuild): pick a
source, choose a file, and it is saved under `uploads/` and ingested in
offline/file mode for that source only (live API creds are ignored for that
run). Accepted per source:

| source | format |
|---|---|
| servicenow | `.csv` (CMDB `cmdb_ci_server` export) |
| rvtools | `.csv` or `.xlsx` (vInfo; vDisk/vNetwork picked up from same dir for CSV) |
| zabbix | `.json` (host.get + utilization shape) |
| prometheus | `.json` (instant-vector shape) |

Upload ingests raw assets only — hit **Rebuild** afterward to re-consolidate
all sources into `Server` records. REST: `POST /api/ingest/upload`
(multipart `source` + `file`).

## Safety

- The agent defaults to **plan mode** (`--permission-mode plan`) — read-only,
  no file/bash side-effects. `--execute` adds `--dangerously-skip-permissions`
  and is opt-in (the CLI prints a red warning).
- Every claim in a `Server` record carries provenance (`source_refs`) back to
  the source row that produced it.
- Agent tasks are persisted in MariaDB (prompt, mode, status, output, error).

## Tests

```bash
python -m pytest tests/ -q     # 12 tests, no network/subprocess
```

## Scaling to 15K servers / 200K cores

- MariaDB handles 15K rows trivially; the connection pool (`pymysql`,
  DictCursor, autocommit) supports multi-writer / concurrent agent execution.
- Ingest is per-source and parallelizable; adapters page through APIs
  (`IDC_SERVICENOW_LIMIT`, Zabbix batched `host.get`).
- The MigraQ RAG retriever (`idc/llm/client.py::_retrieve`) is keyword-based —
  swap for a vector store at 15K scale.
- Agent context is capped at 200 servers (`build_context`); use
  `--focus`/`focus_server_ids` to scope per task.