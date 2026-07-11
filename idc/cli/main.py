"""``idc`` CLI — same core as the web backend, for terminal/automation use.

Examples:
  idc ingest --source all
  idc rebuild
  idc stats
  idc inventory --role db
  idc inventory --format csv > servers.csv
  idc match --explain
  idc waves
  idc ask "which servers run mysql?"
  idc agent "draft a terraform module for the LZ VPC" --stream
  idc serve --port 8010
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from ..config import ROOT, get_settings, load_dotenv
from ..core import open_store, rebuild, run_ingest

load_dotenv()
console = Console()
app_cli = typer.Typer(add_completion=False, help="idc-migrate: IDC→Tencent Cloud migration copilot")


def _store():
    return open_store(get_settings().sqlite_path)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
@app_cli.command()
def ingest(source: str = typer.Option("all", "--source", "-s",
                                      help="all|servicenow|rvtools|zabbix|prometheus")):
    """Pull raw assets from inventory sources into the store."""
    st = _store()
    runs = run_ingest(st, get_settings(), source)
    t = Table("source", "mode", "raw_count", "error")
    for r in runs:
        t.add_row(r.source, r.mode, str(r.raw_count), r.error or "")
    console.print(t)
    total = sum(r.raw_count for r in runs)
    console.print(f"[green]Ingested {total} raw assets from {len(runs)} source(s).[/green]")
    st.close()


@app_cli.command("rebuild")
def rebuild_cmd(do_match: bool = True, do_plan: bool = True):
    """Normalize raw assets → servers, run matching, build wave plan."""
    st = _store()
    stats = rebuild(st, get_settings(), do_match=do_match, do_plan=do_plan)
    console.print(f"[green]Rebuilt:[/green] {json.dumps(stats, ensure_ascii=False)}")
    st.close()


@app_cli.command()
def reset():
    """Wipe the local DB (raw + derived). Confirm with --yes."""
    s = get_settings()
    p = s.sqlite_path
    for suffix in ("", "-wal", "-shm"):
        f = str(p) + suffix
        if os.path.exists(f):
            os.remove(f)
    console.print(f"[yellow]Removed {p}[/yellow]")


@app_cli.command()
def stats():
    """Show inventory/match/wave counts."""
    st = _store()
    console.print_json(data=st.stats())
    st.close()


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------
@app_cli.command()
def inventory(role: Optional[str] = None, env: Optional[str] = None,
              status: Optional[str] = None, source_type: Optional[str] = None,
              os: Optional[str] = None, criticality: Optional[str] = None,
              cluster: Optional[str] = None, target: Optional[str] = None,
              q: Optional[str] = None,
              util_mem_min: Optional[float] = None, conf_max: Optional[float] = None,
              page: int = typer.Option(1, "--page"),
              limit: int = typer.Option(50, "--limit", "-n", help="page size (max 500)"),
              order: str = typer.Option("hostname", "--order"),
              desc: bool = typer.Option(False, "--desc", help="descending"),
              fmt: str = typer.Option("table", "--format", "-f", help="table|json|csv"),
              all_pages: bool = typer.Option(False, "--all", help="csv: stream all matching")):
    """List unified servers (paginated, filtered, faceted) with match target."""
    from ..core.db import ServerFilter
    st = _store()
    f = ServerFilter(role=role, env=env, status=status, source_type=source_type,
                     os=os, criticality=criticality, cluster=cluster,
                     target_product=target, q=q, util_mem_min=util_mem_min, conf_max=conf_max)
    if fmt == "csv":
        import csv as _csv
        w = _csv.writer(sys.stdout)
        w.writerow(["hostname", "role", "type", "os", "cpu", "mem_gb", "disk_gb",
                    "env", "criticality", "target", "spec", "region", "conf"])
        if all_pages:
            for row in st.stream_servers_csv(f, order_by=order):
                # stream_servers_csv yields full CSV including header; pass through
                sys.stdout.write(row)
        else:
            res = st.query_servers(f, page=page, page_size=min(limit, 500),
                                   order_by=order, order_dir="desc" if desc else "asc")
            for s in res["items"]:
                m = st.get_match(s.id)
                w.writerow([s.hostname, s.role, s.source_type, s.os, s.cpu_cores,
                            s.mem_gb, s.total_disk_gb, s.env, s.business_criticality,
                            m.target.product if m else "", m.target.spec if m else "",
                            m.target.region if m else "", f"{m.confidence}" if m else ""])
            console.print(f"[dim]page {res['page']} of ~{max(1,(res['total']-1)//min(limit,500)+1)} | {res['total']} total[/dim]", file=sys.stderr)
        st.close(); return
    res = st.query_servers(f, page=page, page_size=min(limit, 500),
                           order_by=order, order_dir="desc" if desc else "asc")
    servers = res["items"]
    if fmt == "json":
        out = []
        for s in servers:
            d = s.to_dict(); m = st.get_match(s.id)
            d["target"] = m.target.to_dict() if m else None
            out.append(d)
        console.print_json(data={"items": out, "total": res["total"], "page": res["page"],
                                 "facets": res["facets"]})
    else:
        t = Table("hostname", "role", "type", "os", "cpu", "mem", "disk", "env", "crit",
                  "target", "spec", "conf")
        for s in servers:
            m = st.get_match(s.id)
            t.add_row(s.hostname, s.role, s.source_type, s.os, str(s.cpu_cores),
                      str(s.mem_gb), str(s.total_disk_gb), s.env, s.business_criticality,
                      (m.target.product if m else "-"), (m.target.spec if m else "-")[:24],
                      (f"{m.confidence}" if m else "-"))
        console.print(t)
        console.print(f"[dim]page {res['page']} of ~{max(1,(res['total']-1)//min(limit,500)+1)} | {res['total']} total[/dim]")
        fc = res["facets"]
        console.print(f"[dim]facets: role={fc['role']} env={fc['env']} target={fc['target_product']}[/dim]")
    st.close()


@app_cli.command()
def server(sid: str):
    """Show one server in full (with provenance + match + utilization)."""
    st = _store()
    s = st.get_server(sid)
    if not s:
        console.print(f"[red]not found: {sid}[/red]"); raise typer.Exit(1)
    d = s.to_dict()
    m = st.get_match(sid)
    d["match"] = m.to_dict() if m else None
    console.print_json(data=d)
    st.close()


# ---------------------------------------------------------------------------
# matching + waves
# ---------------------------------------------------------------------------
@app_cli.command()
def match(explain: bool = typer.Option(False, "--explain", help="Add LLM explanation per match"),
          top: int = typer.Option(0, "--top", help="Only explain top N (LLM calls cost)")):
    """List server→target matches; optionally LLM-explain each."""
    st = _store()
    servers = {s.id: s for s in st.list_servers()}
    matches = st.list_matches()
    t = Table("server", "product", "spec", "region/az", "conf", "method", "rationale")
    rows = []
    for m in matches:
        s = servers.get(m.server_id)
        rows.append((s.hostname if s else m.server_id, m))
    rows.sort(key=lambda r: r[0])
    from ..llm import get_client
    llm = get_client() if explain else None
    for i, (hn, m) in enumerate(rows):
        rationale = m.rationale
        if explain and (top == 0 or i < top):
            s = servers.get(m.server_id)
            if s:
                rationale = llm.explain_match(s, m)
        t.add_row(hn, m.target.product, m.target.spec, f"{m.target.region}/{m.target.az}",
                  f"{m.confidence}", m.method, rationale[:160])
    console.print(t)
    st.close()


@app_cli.command()
def waves():
    """List migration waves (with members + dependencies)."""
    st = _store()
    servers = {s.id: s for s in st.list_servers(limit=200000)}
    t = Table("wave", "stage", "members", "depends_on", "rationale")
    for w in st.list_waves():
        members = ", ".join(servers[sid].hostname for sid in w.server_ids if sid in servers)
        t.add_row(w.name, w.stage, members or "-", ", ".join(w.depends_on), w.rationale[:80])
    console.print(t)
    st.close()


@app_cli.command("plan-llm")
def plan_llm(demand: str = typer.Argument(..., help="Natural-language wave-plan demand"),
             focus: Optional[List[str]] = typer.Option(None, "--focus", help="hostname to scope the plan to"),
             role: Optional[str] = None, env: Optional[str] = None,
             target: Optional[str] = None, q: Optional[str] = None,
             apply: bool = typer.Option(False, "--apply", help="persist the plan (replaces waves)"),
             fmt: str = typer.Option("table", "--format", "-f", help="table|json")):
    """Build a wave plan from a natural-language demand via the LLM.

    The LLM produces a wave POLICY (a few stages); the deterministic engine
    instantiates it over the estate. Validated before optional --apply.

    Example:
      idc plan-llm "prod DBs first, then apps grouped by app (max 60/wave), \\
      hadoop before web cutover, non-prod last" --apply
    """
    from ..core.db import ServerFilter
    from ..core.llm_plan import build_from_policy, validate_plan, apply_plan
    from ..llm import get_planner
    st = _store()
    servers = st.list_all_servers()
    wls = st.list_workloads()
    matches = st.list_matches()
    scope_ids = None
    if focus:
        by_host = {s.hostname.lower(): s.id for s in servers}
        scope_ids = {by_host[h.lower()] for h in focus if h.lower() in by_host}
        console.print(f"[dim]scoped to {len(scope_ids)} focused servers[/dim]")
    elif any([role, env, target, q]):
        f = ServerFilter(role=role, env=env, target_product=target, q=q)
        scope_ids = set(st.query_server_ids(f))
        console.print(f"[dim]scoped to {len(scope_ids)} filtered servers[/dim]")
    console.print(f"[dim]asking LLM to design a policy for {len(servers if scope_ids is None else scope_ids)} servers…[/dim]")
    res = get_planner().propose(demand, servers, wls, matches, scope_ids)
    pol = res.get("policy")
    if not pol:
        console.print(f"[red]No policy produced: {res.get('errors')}[/red]")
        console.print(f"[dim]raw: {res.get('raw','')[:600]}[/dim]"); raise typer.Exit(1)
    if res.get("errors"):
        console.print(f"[yellow]schema errors: {res['errors']}[/yellow]")
    rep = build_from_policy(pol, servers, wls, matches, scope_ids)
    val = validate_plan(rep.waves, servers)
    if fmt == "json":
        console.print_json(data={"policy": {"notes": pol.notes, "stages": [vars(s) for s in pol.stages]},
                                 "waves": [w.to_dict() for w in rep.waves],
                                 "stats": {"total": rep.total, "assigned": len(rep.assigned), "waves": len(rep.waves)},
                                 "validation": val})
    else:
        console.print(f"[bold green]Policy[/bold green]: {pol.notes}")
        t = Table("label", "stage", "roles", "target", "group_by", "max", "deps")
        for s in pol.stages:
            t.add_row(s.label, s.stage, ",".join(s.roles), ",".join(s.target_products),
                      s.group_by, str(s.max_per_wave), ",".join(s.depends_on))
        console.print(t)
        console.print(f"[bold]Engine[/bold]: {len(rep.waves)} waves, "
                      f"{len(rep.assigned)}/{rep.total} assigned, "
                      f"errors={rep.errors} warnings={rep.warnings}")
        console.print(f"[bold]Validation[/bold]: ok={val['ok']} errors={val['errors']} warnings={val['warnings']}")
        # sample waves
        t2 = Table("wave", "stage", "n", "deps")
        for w in rep.waves[:15]:
            t2.add_row(w.name, w.stage, str(len(w.server_ids)), str(len(w.depends_on)))
        console.print(t2)
        if len(rep.waves) > 15:
            console.print(f"[dim]…and {len(rep.waves)-15} more[/dim]")
    if apply:
        if val["errors"]:
            console.print(f"[red]not applied — validation errors: {val['errors']}[/red]"); raise typer.Exit(1)
        apply_plan(st, rep.waves)
        console.print(f"[green]Applied: {len(rep.waves)} waves persisted (replaced prior plan).[/green]")
    st.close()


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
@app_cli.command()
def ask(question: str = typer.Argument(..., help="Question over the inventory")):
    """Ask a natural-language question about the estate (RAG over inventory)."""
    st = _store()
    from ..llm import get_client
    ans = get_client().ask(question, st.list_servers(), st.list_matches())
    console.print(f"[bold]Q:[/bold] {question}\n")
    console.print(ans)
    st.close()


@app_cli.command()
def explain(sid: str):
    """LLM-explain the match for one server."""
    st = _store()
    s = st.get_server(sid)
    if not s:
        console.print(f"[red]not found: {sid}[/red]"); raise typer.Exit(1)
    m = st.get_match(sid)
    if not m:
        console.print("[red]no match[/red]"); raise typer.Exit(1)
    from ..llm import get_client
    console.print(f"[bold]{s.hostname}[/bold] → {m.target.product} {m.target.spec}")
    console.print(get_client().explain_match(s, m))
    st.close()


# ---------------------------------------------------------------------------
# Claude Code agent
# ---------------------------------------------------------------------------
@app_cli.command()
def agent(task: str = typer.Argument(..., help="Task for the Claude Code agent"),
          execute: bool = typer.Option(False, "--execute", help="Allow file/bash side-effects (dangerous)"),
          focus: Optional[List[str]] = typer.Option(None, "--focus", help="hostname to focus context on"),
          stream: bool = typer.Option(True, "--stream/--no-stream"),
          timeout: Optional[int] = None):
    """Run an agentic migration task via the Claude Code CLI."""
    st = _store()
    from ..agent import build_context, stream_agent
    servers = st.list_servers()
    # resolve focus hostnames → ids
    focus_ids = None
    if focus:
        by_host = {s.hostname.lower(): s.id for s in servers}
        focus_ids = [by_host[h.lower()] for h in focus if h.lower() in by_host]
    ctx = build_context(servers, st.list_matches(), st.list_waves(), focus_server_ids=focus_ids)
    mode = "execute" if execute else "plan"
    if execute:
        console.print("[red bold]EXECUTE MODE: agent may write files / run shell commands.[/red bold]")

    async def _run():
        async for ev in stream_agent(task, settings=get_settings(), mode=mode,
                                     context=ctx, timeout=timeout):
            if ev.kind == "text" and stream:
                console.print(ev.text, end="")
            elif ev.kind == "tool":
                console.print(f"\n[dim]{ev.text}[/dim]")
            elif ev.kind == "result":
                if not stream:
                    console.print(ev.text)
                console.print(f"\n[green]\n[done][/green]")
            elif ev.kind == "error":
                console.print(f"\n[red]{ev.text}[/red]")
    asyncio.run(_run())
    st.close()


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
@app_cli.command()
def serve(host: str = "0.0.0.0", port: int = 8010):
    """Start the web backend (FastAPI)."""
    import uvicorn
    console.print(f"[green]idc-migrate web → http://{host}:{port}[/green]")
    uvicorn.run("idc.backend.app:app", host=host, port=port, reload=False, log_level="info")


@app_cli.command()
def doctor():
    """Check config, DB, LLM gateway, and claude CLI availability."""
    s = get_settings()
    console.print(f"[bold]config[/bold]")
    console.print(f"  db           {s.sqlite_path}")
    console.print(f"  servicenow   {'online' if s.has_servicenow() else 'fixture'}")
    console.print(f"  zabbix       {'online' if s.has_zabbix() else 'fixture'}")
    console.print(f"  prometheus   {'online' if s.has_prometheus() else 'fixture'}")
    console.print(f"  rvtools      {s.rvtools_path}")
    console.print(f"  llm          {s.llm_base} model={s.llm_model} enabled={s.llm_enabled}")
    console.print(f"  claude       bin={s.claude_bin} default_mode={s.claude_default_mode}")
    # checks
    import shutil, httpx
    console.print(f"[bold]checks[/bold]")
    console.print(f"  claude bin   {'OK' if shutil.which(s.claude_bin) else 'MISSING'}")
    try:
        r = httpx.get(f"{s.llm_base}/api/tags", timeout=5)
        console.print(f"  llm gateway  OK ({r.status_code})")
    except Exception as e:
        console.print(f"  llm gateway  FAIL ({e!r})")
    st = _store()
    console.print(f"  db           OK servers={st.count_servers()}")
    st.close()


def main():
    app_cli()


if __name__ == "__main__":
    main()