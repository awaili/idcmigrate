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
    return open_store(get_settings().db_url)


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
def rebuild_cmd(do_match: bool = True, do_plan: bool = True,
                max_waves: int = typer.Option(0, "--max-waves",
                    help="hard cap on total waves (0 = no cap). Unifies the guarantee with plan-llm.")):
    """Normalize raw assets → servers, run matching, build wave plan."""
    st = _store()
    stats = rebuild(st, get_settings(), do_match=do_match, do_plan=do_plan, max_waves=max_waves)
    console.print(f"[green]Rebuilt:[/green] {json.dumps(stats, ensure_ascii=False)}")
    st.close()


@app_cli.command()
def reset():
    """Wipe the DB (raw + derived): drop & recreate the MariaDB database."""
    s = get_settings()
    url = s.db_url
    from urllib.parse import urlparse, unquote
    import pymysql
    p = urlparse(url)
    db = p.path.lstrip("/")
    root = pymysql.connect(host=p.hostname, port=p.port or 3306,
                           user=p.username, password=unquote(p.password or ""),
                           charset="utf8mb4", autocommit=True)
    with root.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        cur.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    root.close()
    console.print(f"[yellow]Dropped & recreated MariaDB database `{db}` on {p.hostname}[/yellow]")


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
             max_waves: Optional[int] = typer.Option(None, "--max-waves",
                help="hard cap on total instantiated waves; if exceeded the LLM is re-prompted to revise. Also parsed from the demand (e.g. '最多10个wave')"),
             apply: bool = typer.Option(False, "--apply", help="persist the plan (replaces waves)"),
             fmt: str = typer.Option("table", "--format", "-f", help="table|json")):
    """Build a wave plan from a natural-language demand via the LLM.

    The LLM produces a wave POLICY (a few stages); the deterministic engine
    instantiates it over the estate. Validated before optional --apply.

    --max-waves enforces a cap on the final wave count: if the engine produces
    more waves than the cap, the LLM is re-prompted (up to 2 rounds) to revise
    the policy. The cap is also auto-parsed from phrases like "最多10个wave".

    Example:
      idc plan-llm "prod DBs first, then apps grouped by app (max 60/wave), \\
      hadoop before web cutover, non-prod last" --max-waves 10 --apply
    """
    from ..core.db import ServerFilter
    from ..core.llm_plan import validate_plan, apply_plan
    from ..llm import get_planner
    st = _store()
    servers = st.list_all_servers()
    wls = st.list_workloads()
    matches = st.list_matches()
    profiles = st.list_code_profiles()
    strategies = {s.app_id: s for s in st.list_app_strategies()}
    scope_ids = None
    if focus:
        by_host = {s.hostname.lower(): s.id for s in servers}
        scope_ids = {by_host[h.lower()] for h in focus if h.lower() in by_host}
        console.print(f"[dim]scoped to {len(scope_ids)} focused servers[/dim]")
    elif any([role, env, target, q]):
        f = ServerFilter(role=role, env=env, target_product=target, q=q)
        scope_ids = set(st.query_server_ids(f))
        console.print(f"[dim]scoped to {len(scope_ids)} filtered servers[/dim]")
    cap_note = f", cap={max_waves}" if max_waves else ""
    console.print(f"[dim]asking LLM to design a policy for {len(servers if scope_ids is None else scope_ids)} servers{cap_note}…[/dim]")
    res = get_planner().propose(demand, servers, wls, matches, scope_ids,
                                max_waves=max_waves, profiles=profiles,
                                strategies=strategies)
    pol = res.get("policy")
    if not pol:
        console.print(f"[red]No policy produced: {res.get('errors')}[/red]")
        console.print(f"[dim]raw: {res.get('raw','')[:600]}[/dim]"); raise typer.Exit(1)
    if res.get("errors"):
        console.print(f"[yellow]schema errors: {res['errors']}[/yellow]")
    if res.get("revisions"):
        for r in res["revisions"]:
            console.print(f"[dim]revision #{r['attempt']}: {r['waves_before']} -> "
                          f"{r['waves_after']} waves (cap {res.get('max_waves')}) "
                          f"accepted={r['accepted']}[/dim]"
                          + (f" [yellow]{r['errors']}[/yellow]" if r.get('errors') else ""))
    # propose returns the authoritative, already-capped waves — use them directly
    # (no re-instantiation: the cap + 7R exclusion can't be forgotten downstream).
    waves = res.get("waves") or []
    val = validate_plan(waves, servers)
    total = len(scope_ids) if scope_ids is not None else len(servers)
    assigned = len({sid for w in waves for sid in w.server_ids})
    if fmt == "json":
        console.print_json(data={"policy": {"notes": pol.notes, "stages": [vars(s) for s in pol.stages]},
                                 "waves": [w.to_dict() for w in waves],
                                 "stats": {"total": total, "assigned": assigned, "waves": len(waves)},
                                 "max_waves": res.get("max_waves"),
                                 "revisions": res.get("revisions", []),
                                 "engine_capped": res.get("engine_capped"),
                                 "validation": val})
    else:
        console.print(f"[bold green]Policy[/bold green]: {pol.notes}")
        t = Table("label", "stage", "roles", "target", "group_by", "max", "deps")
        for s in pol.stages:
            t.add_row(s.label, s.stage, ",".join(s.roles), ",".join(s.target_products),
                      s.group_by, str(s.max_per_wave), ",".join(s.depends_on))
        console.print(t)
        console.print(f"[bold]Engine[/bold]: {len(waves)} waves, "
                      f"{assigned}/{total} assigned, "
                      f"errors={res.get('errors') or []} warnings={res.get('warnings') or []}")
        console.print(f"[bold]Validation[/bold]: ok={val['ok']} errors={val['errors']} warnings={val['warnings']}")
        # sample waves
        t2 = Table("wave", "stage", "n", "deps")
        for w in waves[:15]:
            t2.add_row(w.name, w.stage, str(len(w.server_ids)), str(len(w.depends_on)))
        console.print(t2)
        if len(waves) > 15:
            console.print(f"[dim]…and {len(waves)-15} more[/dim]")
    if apply:
        if val["errors"]:
            console.print(f"[red]not applied — validation errors: {val['errors']}[/red]"); raise typer.Exit(1)
        apply_plan(st, waves)
        console.print(f"[green]Applied: {len(waves)} waves persisted (replaced prior plan).[/green]")
    st.close()


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
@app_cli.command()
def ask(question: str = typer.Argument(..., help="Question over the inventory")):
    """Ask a natural-language question about the estate.

    Grounded: the question is classified into an intent, a deterministic engine
    computes the answer (deps, what-if, riskiest waves, aggregates, retain/retire,
    server search), then the LLM narrates it. Falls back to keyword RAG if the
    grounded path fails. Try: 'what if app-X is delayed?', 'which are the
    riskiest waves?', 'how many prod servers are in cutover?'.
    """
    st = _store()
    from ..llm import get_client
    client = get_client()
    res = client.ask_grounded(question, st.list_all_servers(), st.list_waves(),
                              st.list_workloads(), st.list_matches(),
                              st.list_code_profiles(),
                              {s.app_id: s for s in st.list_app_strategies()})
    console.print(f"[bold]Q:[/bold] {question}")
    if res.get("ok"):
        console.print(f"[dim]intent: {res['intent']}  params: {res.get('params')}[/dim]")
        console.print(res["answer"])
    else:
        # fallback to keyword RAG
        ans = client.ask(question, st.list_servers(), st.list_matches())
        console.print(f"[dim](grounded failed: {res.get('answer')} — keyword fallback)[/dim]")
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


@app_cli.command()
def audit(sid: str):
    """AI second-opinion on the rule-based migration target for a server.

    Critiques whether the rule engine picked the right Tencent target, suggests
    an alternative if not (e.g. a db-role server mapped to CVM should be CDB),
    and lists migration risks. Advisory only — does not change the stored match.
    """
    st = _store()
    s = st.get_server(sid)
    if not s:
        console.print(f"[red]not found: {sid}[/red]"); raise typer.Exit(1)
    m = st.get_match(sid)
    if not m:
        console.print("[red]no match[/red]"); raise typer.Exit(1)
    from ..llm import get_client
    prof = None
    if s.app_ids:
        pmap = {p.app_id: p for p in st.list_code_profiles()}
        prof = next((pmap[a] for a in s.app_ids if a in pmap), None)
    console.print(f"[bold]{s.hostname}[/bold] ({s.role}, {s.os}) → rule: "
                  f"{m.target.product} {m.target.spec} [conf {m.confidence:.2f}]")
    r = get_client().audit_match(s, m, prof)
    if not r.get("ok"):
        console.print(f"[red]audit failed: {r.get('error')}[/red]"); raise typer.Exit(1)
    color = {"keep": "green", "change": "yellow", "review": "magenta"}[r["verdict"]]
    console.print(f"[bold {color}]verdict: {r['verdict']}[/bold {color}] "
                  f"(conf {r['confidence']:.2f})")
    if r["verdict"] == "change":
        console.print(f"[bold]alternative:[/bold] {r['alternative_target']} "
                       f"{r.get('alternative_spec') or ''}")
    if r["critique"]:
        console.print(f"critique: {r['critique']}")
    if r["rationale"]:
        console.print(f"rationale: {r['rationale']}")
    if r["risks"]:
        console.print("[bold]risks:[/bold]")
        for risk in r["risks"]:
            console.print(f"  • {risk}")
    st.close()


@app_cli.command("wave-assess")
def wave_assess(wave_id: str):
    """Risk score + auto runbook for one migration wave.

    The risk score/level/factors are deterministic (reproducible); the
    go/no-go + runbook (pre_checks / cutover / rollback) + summary are
    LLM-generated, grounded in the wave's members + targets + code profiles.
    """
    st = _store()
    w = next((x for x in st.list_waves() if x.id == wave_id), None)
    if not w:
        console.print(f"[red]wave not found: {wave_id}[/red]"); raise typer.Exit(1)
    from ..llm import get_client
    r = get_client().assess_wave(w, st.list_all_servers(), st.list_matches(),
                                 st.list_code_profiles())
    color = {"low": "green", "medium": "yellow", "high": "red"}.get(r["risk_level"], "white")
    console.print(f"[bold]{w.name}[/bold] ({w.stage}, {len(w.server_ids)} servers, "
                  f"depends_on {len(w.depends_on)})")
    console.print(f"[bold {color}]risk: {r['risk_level']} ({r['risk_score']})[/bold {color}]")
    for f in r["risk_factors"]:
        console.print(f"  • {f}")
    if r.get("go_no_go"):
        gc = {"go": "green", "hold": "yellow", "no-go": "red"}.get(r["go_no_go"], "white")
        console.print(f"[bold {gc}]go/no-go: {r['go_no_go']}[/bold {gc}]")
    rb = r.get("runbook") or {}
    for sec in ("pre_checks", "cutover", "rollback"):
        items = rb.get(sec) or []
        if items:
            console.print(f"[bold]{sec}:[/bold]")
            for it in items:
                console.print(f"  • {it}")
    if r.get("summary"):
        console.print(f"[bold]summary:[/bold] {r['summary']}")
    if not r.get("ok"):
        console.print(f"[yellow](LLM unavailable: {r.get('error')} — risk score above is still valid)[/yellow]")
    st.close()


@app_cli.command("plan-review")
def plan_review():
    """Red-team the current wave plan for semantic issues (env mixing,
    criticality placement, blast radius, dependency-order, retain/retire
    leak, coverage). Structural validity is pre-checked separately; this is
    the AI semantic pass. Advisory only."""
    from ..core.llm_plan import validate_plan
    st = _store()
    waves = st.list_waves()
    if not waves:
        console.print("[red]no waves to review — build a plan first[/red]"); raise typer.Exit(1)
    servers = st.list_all_servers()
    val = validate_plan(waves, servers)
    if val["errors"]:
        console.print(f"[red]structural errors — fix before review: {val['errors']}[/red]")
        raise typer.Exit(1)
    from ..llm import get_client
    strategies = {s.app_id: s for s in st.list_app_strategies()}
    r = get_client().review_plan(waves, servers, st.list_workloads(), st.list_matches(),
                                 st.list_code_profiles(), strategies)
    if not r.get("ok"):
        console.print(f"[red]review failed: {r.get('error')}[/red]"); raise typer.Exit(1)
    color = {"sound": "green", "needs-work": "yellow", "risky": "red"}.get(r["overall"], "white")
    console.print(f"[bold {color}]overall: {r['overall']}[/bold {color}] "
                  f"({len(waves)} waves, {len(r['findings'])} finding(s))")
    if r["findings"]:
        t = Table("sev", "wave", "issue", "suggestion")
        for f in r["findings"]:
            sc = {"high": "red", "medium": "yellow", "low": "white"}.get(f["severity"], "white")
            t.add_row(f"[{sc}]{f['severity']}[/{sc}]", f["wave"],
                      (f["issue"][:60] + "…" if len(f["issue"]) > 60 else f["issue"]),
                      (f["suggestion"][:50] + "…" if len(f["suggestion"]) > 50 else f["suggestion"]))
        console.print(t)
    if r.get("summary"):
        console.print(f"[bold]summary:[/bold] {r['summary']}")
    st.close()


@app_cli.command()
def strategy(app_id: str = typer.Argument(..., help="app_id to assign a 7R strategy to"),
             all: bool = typer.Option(False, "--all", help="run for every app in the workloads graph"),
             apply: bool = typer.Option(False, "--apply", help="persist the chosen strategy into the app's CodeProfile.migration_pattern"),
             fmt: str = typer.Option("table", "--format", "-f", help="table|json")):
    """Assign a 7R migration strategy (6R + rehost-container) per app via the LLM.

    Runs after reconsolidate: takes an app's consolidated context (servers,
    workload/deps, matched Tencent targets, code profile) and asks the LLM to
    pick one of rehost / rehost-container / replatform / refactor / repurchase
    / retain / retire, with rationale + target + effort + key changes.

    --apply persists the chosen strategy into the ``app_strategies`` table
    (separate from ``CodeProfile.migration_pattern`` — the executor's scan
    conclusion is NOT overwritten). The wave planners read app_strategies as a
    7R overlay, so retain/retire then drive wave exclusion on the next
    Rebuild / plan-llm. --all iterates every workload app_id.
    """
    from ..llm import get_client
    from ..core.models import AppStrategy, STRATEGY_SOURCE_AI, _now
    st = _store()
    servers = st.list_all_servers()
    wls = st.list_workloads()
    matches = st.list_matches()
    profiles = st.list_code_profiles()
    app_ids = [w.app_id for w in wls] if all else [app_id]
    if not all and app_id not in {w.app_id for w in wls}:
        console.print(f"[yellow]warning: {app_id} not in the workload graph[/yellow]")
    client = get_client()
    results = []
    for aid in app_ids:
        r = client.seven_r_strategy(aid, servers, wls, matches, profiles)
        results.append(r)
        if apply and r.get("ok"):
            st.upsert_app_strategy(AppStrategy(
                app_id=aid, strategy=r["strategy"], rationale=r["rationale"],
                target=r["target"], confidence=r["confidence"], effort=r["effort"],
                key_changes=r["key_changes"], source=STRATEGY_SOURCE_AI,
                assigned_at=_now(), updated_at=_now()))
            r["applied"] = True
    # output
    if fmt == "json":
        console.print_json(data=results)
    else:
        t = Table("app_id", "strategy", "target", "effort", "conf", "rationale", "applied")
        for r in results:
            if r.get("ok"):
                t.add_row(r["app_id"], r["strategy"], r["target"], r["effort"],
                          f"{r['confidence']:.2f}", (r["rationale"][:60] + "…" if len(r.get("rationale",""))>60 else r.get("rationale","")),
                          str(r.get("applied", "-")))
            else:
                t.add_row(r["app_id"], "[red]—[/red]", "", "", "", f"[red]{r.get('error','')}[/red]", "-")
        console.print(t)
        if not all:
            r0 = results[0]
            if r0.get("ok") and r0.get("key_changes"):
                console.print("[bold]key changes:[/bold]")
                for kc in r0["key_changes"]:
                    console.print(f"  • {kc}")
    n_ok = sum(1 for r in results if r.get("ok"))
    console.print(f"[dim]{n_ok}/{len(results)} assigned a 7R strategy"
                  + (f", {sum(1 for r in results if r.get('applied'))} applied" if apply else "")
                  + "[/dim]")
    st.close()


# ---------------------------------------------------------------------------
# F2 — TCO / business case
# ---------------------------------------------------------------------------
@app_cli.command()
def cost(format: str = typer.Option("table", "--format", help="table | json | csv"),
         top: int = typer.Option(20, "--top", help="top-N most expensive servers")):
    """Per-server cloud run-cost estimate (F2). Run after `rebuild`."""
    from ..core.cost import estimate_portfolio, load_pricebook
    st = _store()
    servers = st.list_all_servers()
    matches = st.list_matches()
    strategies = {s.app_id: s for s in st.list_app_strategies()}
    from ..core.cost import strategies_from_tags
    for app_id, strat in strategies_from_tags(servers).items():
        strategies[app_id] = strat
    book = load_pricebook(get_settings())
    bc = estimate_portfolio(servers, matches, book, strategies=strategies)
    st.close()
    if format == "json":
        console.print_json(json.dumps(bc, ensure_ascii=False)); return
    console.print(f"[bold]cloud run cost[/bold]  "
                  f"monthly=${bc['cloud_monthly']:.0f}  yearly=${bc['cloud_yearly']:.0f}  "
                  f"priced={bc['priced_servers']}/{bc['priced_servers']+bc['unpriced_servers']}  "
                  f"source={bc['pricing_source']}")
    if bc.get("annual_savings") is not None:
        console.print(f"  on-prem yearly=${bc['onprem_yearly']:.0f}  "
                      f"[green]annual savings=${bc['annual_savings']:.0f}[/green]")
    t = Table("hostname", "app", "product", "region", "strategy", "monthly$", "yearly$", "basis")
    for r in sorted(bc["per_server"], key=lambda x: -x["yearly"])[:top]:
        t.add_row(r["hostname"], ",".join(r["app_ids"]) or "-", r["product"],
                  r["region"], r["strategy"], f"{r['monthly']:.0f}", f"{r['yearly']:.0f}",
                  r["basis"])
    console.print(t)


@app_cli.command("business-case")
def business_case(save: bool = typer.Option(True, "--save/--no-save"),
                  format: str = typer.Option("text", "--format", help="text | json")):
    """Portfolio TCO + per-strategy rollup (F2). Saves a snapshot by default."""
    from ..core.cost import estimate_portfolio, load_pricebook
    from ..core.models import _new_id
    st = _store()
    servers = st.list_all_servers()
    matches = st.list_matches()
    strategies = {s.app_id: s for s in st.list_app_strategies()}
    from ..core.cost import strategies_from_tags
    for app_id, strat in strategies_from_tags(servers).items():
        strategies[app_id] = strat
    book = load_pricebook(get_settings())
    bc = estimate_portfolio(servers, matches, book, strategies=strategies)
    sid = ""
    if save:
        sid = _new_id("bc")
        st.save_business_case(sid, bc)
        bc["snapshot_id"] = sid
    st.close()
    if format == "json":
        console.print_json(json.dumps(bc, ensure_ascii=False)); return
    console.print(f"[bold]Business case[/bold]  source={bc['pricing_source']}"
                  + (f"  snapshot={sid}" if sid else ""))
    console.print(f"  cloud:  monthly ${bc['cloud_monthly']:.0f}  yearly ${bc['cloud_yearly']:.0f}")
    if bc.get("annual_savings") is not None:
        console.print(f"  on-prem yearly ${bc['onprem_yearly']:.0f}  "
                      f"[green]annual savings ${bc['annual_savings']:.0f}[/green]")
    console.print("[bold]per strategy[/bold]")
    ts = Table("strategy", "servers", "yearly$")
    for k in sorted(bc["per_strategy"]):
        v = bc["per_strategy"][k]
        ts.add_row(k, str(v["servers"]), f"{v['yearly']:.0f}")
    console.print(ts)
    console.print("[bold]per product[/bold]")
    tp = Table("product", "yearly$")
    for k, v in sorted(bc["per_product"].items(), key=lambda x: -x[1]):
        tp.add_row(k, f"{v:.0f}")
    console.print(tp)


@app_cli.command("readiness")
def readiness_cmd(wave: Optional[str] = typer.Option(None, "--wave", "-w",
                  help="single wave id; omit for the whole portfolio"),
                  fmt: str = typer.Option("table", "--format", "-f",
                  help="table | json")):
    """Portfolio readiness heatmap (F10): is each wave safe to cut?

    Six signals per wave (lz_ready / db_conversion / code_refactor /
    deps_resolved / cutover_rehearsal / rollback_channel) each green/yellow/red;
    rollup = worst case (red blocks cutover)."""
    from ..core.readiness import wave_readiness, portfolio_readiness
    st = _store()
    _LVL_COLOR = {"green": "green", "yellow": "yellow", "red": "red", "n/a": "dim"}
    if wave:
        rows = [wave_readiness(st, wave)]
        if "error" in rows[0]:
            console.print(f"[red]{rows[0]['error']}[/red]"); st.close(); raise typer.Exit(1)
    else:
        rows = portfolio_readiness(st)
    st.close()
    if fmt == "json":
        console.print_json(json.dumps(rows, ensure_ascii=False)); return
    tbl = Table("wave", "stage", "servers", "lz", "db", "code", "deps", "rehearsal",
                "rollback", "rollup", "cutover?")
    for r in rows:
        sg = r.get("signals", {})
        def _cell(k):
            v = sg.get(k, {})
            c = _LVL_COLOR.get(v.get("level", ""), "dim")
            return f"[{c}]{v.get('level','-')}[/{c}]"
        rc = _LVL_COLOR.get(r.get("rollup", ""), "dim")
        tbl.add_row(r.get("wave_name", r.get("wave_id", ""))[:28], r.get("stage", ""),
                    str(r.get("server_count", 0)),
                    _cell("lz_ready"), _cell("db_conversion"), _cell("code_refactor"),
                    _cell("deps_resolved"), _cell("cutover_rehearsal"),
                    _cell("rollback_channel"),
                    f"[{rc}]{r.get('rollup','-')}[/{rc}]",
                    "yes" if r.get("can_cutover") else "no")
    console.print(tbl)
    # detail lines for any non-green signal in the single-wave view
    if wave:
        for k, v in sg.items():
            if v.get("level") in ("yellow", "red"):
                console.print(f"  [{_LVL_COLOR[v['level']]}]{k}: {v['detail']}[/{_LVL_COLOR[v['level']]}]")


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


@app_cli.command("executor-mock")
def executor_mock(host: str = "0.0.0.0", port: int = 8090):
    """Start the mock external executor (implements the executor side of the
    contract). Point idc-migrate at it with IDC_EXECUTOR_URL=http://localhost:8090."""
    import uvicorn
    console.print(f"[green]mock executor → http://{host}:{port}[/green]")
    console.print(f"[dim]callback target: {os.getenv('IDC_MOCK_CALLBACK','http://localhost:8010')}[/dim]")
    uvicorn.run("idc.executor_mock.app:app", host=host, port=port, reload=False, log_level="info")


@app_cli.command()
def doctor():
    """Check config, DB, LLM gateway, and claude CLI availability."""
    s = get_settings()
    console.print(f"[bold]config[/bold]")
    console.print(f"  db           {s.db_url}")
    console.print(f"  servicenow   {'online' if s.has_servicenow() else 'fixture'}  path={s.sn_path}")
    console.print(f"  zabbix       {'online' if s.has_zabbix() else 'fixture'}  path={s.zbx_path}")
    console.print(f"  prometheus   {'online' if s.has_prometheus() else 'fixture'}  path={s.prom_path}")
    console.print(f"  rvtools      {s.rvtools_path}")
    console.print(f"  llm          {s.llm_base} model={s.llm_model} enabled={s.llm_enabled}")
    console.print(f"  claude       bin={s.claude_bin} default_mode={s.claude_default_mode}")
    console.print(f"  executor     {s.executor_url or '(not configured)'}  "
                  f"enabled={s.executor_enabled}  token={'set' if s.executor_token else 'unset'}")
    console.print(f"  pricing      {'live' if s.has_pricing() else 'fixture'}  "
                  f"url={s.pricing_url or '(bundled)'}  override={'set' if s.pricing_override_path else 'off'}")
    console.print(f"  sms/dts      {'wired' if s.has_sms() else 'track-only'}  "
                  f"base={s.sms_base or '(not configured)'}  region={s.sms_region or '-'}")
    console.print(f"  netdep       {s.netdep_source}  enabled={s.netdep_enabled()}  days={s.netdep_days}")
    # checks
    import shutil, httpx
    from ..agent import get_executor_client, executor_status
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
    es = executor_status(s)
    tag = "OK" if es["reachable"] else ("OFF" if not es["configured"] else "FAIL")
    console.print(f"  executor     {tag}  {es['detail']}"
                  + (f"  v={es['version']}" if es["version"] else ""))


# ---------------------------------------------------------------------------
# code intelligence (external agent executor)
# ---------------------------------------------------------------------------
code_cli = typer.Typer(add_completion=False, help="Code scan/comb/modify via the external agent executor.")
app_cli.add_typer(code_cli, name="code")


@code_cli.command("profiles")
def code_profiles():
    """List code profiles pushed by the executor."""
    st = _store()
    t = Table("app_id", "pattern", "effort", "readiness", "deps", "blockers", "findings", "scanned_at")
    for p in st.list_code_profiles():
        t.add_row(p.app_id, p.migration_pattern or "-", p.refactor_effort or "-",
                  f"{p.cloud_readiness:.2f}" if p.cloud_readiness is not None else "-",
                  ",".join(p.code_deps) or "-", str(len(p.blockers)),
                  str(len(p.findings)), p.scanned_at or "-")
    console.print(t)
    st.close()


@code_cli.command("show")
def code_show(app_id: str = typer.Argument(...)):
    """Show one app's code profile + findings."""
    st = _store()
    p = st.get_code_profile(app_id)
    if not p:
        console.print(f"[red]no profile for {app_id}[/red]")
        st.close()
        raise typer.Exit(1)
    d = p.to_dict()
    console.print_json(data=d)
    st.close()


@code_cli.command("jobs")
def code_jobs():
    """List executor change jobs (audit trail)."""
    st = _store()
    t = Table("id", "app_id", "kind", "status", "patch_ref", "created_at", "error")
    for j in st.list_change_jobs():
        t.add_row(j["id"], j["app_id"], j["kind"], j["status"],
                  j["patch_ref"] or "-", j["created_at"] or "-", (j["error"] or "")[:40])
    console.print(t)
    st.close()


@code_cli.command("scan")
def code_scan(app_id: str = typer.Option(..., "--app"),
              repo_url: str = typer.Option(..., "--repo"),
              branch: str = typer.Option("", "--branch"),
              action: str = typer.Option("scan", "--action", help="scan|comb|modify"),
              mode: str = typer.Option("plan", "--mode", help="plan|execute (modify)"),
              scope: List[str] = typer.Option([], "--scope", help="category to change (repeatable; modify)"),
              override: List[str] = typer.Option([], "--override", help="old=new (repeatable; modify)")):
    """Trigger the external executor to scan/comb/modify an app's repo.

    For ``--action modify``: idc-migrate builds a concrete change list from the
    app's stored CodeProfile, refined by any ``--override old=new`` pairs, and
    sends it so the executor knows exactly what to change. ``--scope`` limits
    which categories to change (repeatable; omit = all).
    """
    from ..agent import ExecutorError, get_executor_client
    s = get_settings()
    if not s.executor_enabled:
        console.print("[red]executor disabled (IDC_EXECUTOR_ENABLED=false)[/red]")
        raise typer.Exit(1)
    ec = get_executor_client(s)
    if not ec.configured:
        console.print("[red]IDC_EXECUTOR_URL not configured[/red]")
        raise typer.Exit(1)
    # parse --override old=new → {old:new}
    overrides = _parse_overrides(override)
    for o in override:
        if "=" not in o:
            console.print(f"[yellow]ignoring malformed --override {o!r} (need old=new)[/yellow]")
    try:
        if action == "modify":
            spec_changes, spec_notes = _build_modify_changes(app_id, scope, overrides)
            res = ec.modify(app_id, repo_url, branch, mode=mode, scope=scope,
                            changes=spec_changes, notes=spec_notes)
        elif action == "comb":
            res = ec.comb(app_id, repo_url, branch)
        else:
            res = ec.scan(app_id, repo_url, branch)
    except ExecutorError as e:
        console.print(f"[red]executor error: {e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]triggered {action} for {app_id}[/green]")
    if action == "modify":
        console.print(f"[dim]sent {len(spec_changes)} concrete change(s) to executor[/dim]")
        for c in spec_changes:
            console.print(f"  · [{c['category']}] {c.get('file','')}:{c.get('line') or '?'}  "
                          f"{c.get('old') or '(?)'} → {c.get('new') or '(?)'}")
        for n in spec_notes:
            console.print(f"[yellow]note: {n}[/yellow]")
    console.print_json(data=res)


def _build_modify_changes(app_id, scope, overrides):
    """Build the concrete change list for a modify trigger from the stored
    CodeProfile + operator overrides. Mirrors backend executor_trigger."""
    from ..core.codeintel import build_change_spec
    st = _store()
    try:
        profile = st.get_code_profile(app_id)
        spec = build_change_spec(profile, scope=scope or None, overrides=overrides)
        return spec.changes, spec.notes
    finally:
        st.close()


def _parse_overrides(items):
    """Parse a list of ``old=new`` strings into a ``{old: new}`` dict. Pure.

    Malformed entries (no ``=``) are skipped — the caller warns about them.
    """
    out = {}
    for o in items or []:
        if "=" in o:
            k, v = o.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


@code_cli.command("questions")
def code_questions(app: Optional[str] = typer.Option(None, "--app", help="filter by app_id"),
                   status: Optional[str] = typer.Option("pending", "--status", help="pending|answered|skipped|all")):
    """List executor→operator questions (default: pending)."""
    st = _store()
    try:
        qs = st.list_questions(app_id=app, status=None if status == "all" else status)
        if not qs:
            console.print(f"[dim]no {status} questions[/dim]"); return
        t = Table("id", "app_id", "kind", "status", "prompt", "loc", "answered")
        for q in qs:
            ctx = q.context or {}
            loc = f"{ctx.get('file','')}:{ctx.get('line','')}" if ctx.get("file") else "-"
            t.add_row(q.id, q.app_id, q.kind, q.status,
                      (q.prompt or "")[:60], loc, q.answer[:30] if q.answer else "-")
        console.print(t)
    finally:
        st.close()


@code_cli.command("answer")
def code_answer(qid: str = typer.Argument(...),
                value: str = typer.Option(..., "--value", help="the answer"),
                by: str = typer.Option("cli", "--by", help="answered_by tag")):
    """Answer a pending question (the executor polls GET /api/questions/{id} and continues)."""
    from ..core.models import _now
    st = _store()
    try:
        q = st.get_question(qid)
        if not q:
            console.print(f"[red]no such question {qid}[/red]"); raise typer.Exit(1)
        if q.status != "pending":
            console.print(f"[red]question already {q.status}[/red]"); raise typer.Exit(1)
        q.status = "answered"; q.answer = value.strip(); q.answered_by = by; q.answered_at = _now()
        st.upsert_question(q)
        console.print(f"[green]answered {qid} → {q.answer}[/green]")
    finally:
        st.close()


@code_cli.command("skip")
def code_skip(qid: str = typer.Argument(...),
              reason: str = typer.Option("", "--reason", help="why skipped")):
    """Dismiss a pending question (the executor will skip that change)."""
    from ..core.models import _now
    st = _store()
    try:
        q = st.get_question(qid)
        if not q:
            console.print(f"[red]no such question {qid}[/red]"); raise typer.Exit(1)
        if q.status != "pending":
            console.print(f"[red]question already {q.status}[/red]"); raise typer.Exit(1)
        q.status = "skipped"; q.answer = reason; q.answered_at = _now()
        st.upsert_question(q)
        console.print(f"[green]skipped {qid}[/green]")
    finally:
        st.close()


# ---------------------------------------------------------------------------
# F6 — migration execution (track-only state machine + validation gates)
# ---------------------------------------------------------------------------
migrate_cli = typer.Typer(add_completion=False, help="Migration execution state machine + validation gates.")
app_cli.add_typer(migrate_cli, name="migrate")


@migrate_cli.command("launch")
def migrate_launch(wave_id: str = typer.Argument(..., help="wave id to launch jobs for"),
                   kind: str = typer.Option("host", "--kind", help="host | db | code")):
    """Create a MigrationJob (status=planned) for each server in the wave."""
    from ..core.execute import launch_wave
    st = _store()
    try:
        jobs = launch_wave(st, wave_id, st.list_all_servers(), st.list_matches(),
                           st.list_workloads(), kind=kind)
        console.print(f"[green]launched {len(jobs)} job(s) for wave {wave_id}[/green]")
        t = Table("job_id", "server_id", "app_id", "kind", "status", "gates")
        for j in jobs:
            t.add_row(j.id, j.server_id, j.app_id or "-", j.kind, j.status,
                      str(len(j.validation_gates)))
        console.print(t)
    finally:
        st.close()


@migrate_cli.command("jobs")
def migrate_jobs(wave_id: str = typer.Option(None, "--wave", help="filter by wave id"),
                 status: str = typer.Option(None, "--status", help="filter by status")):
    """List migration jobs."""
    st = _store()
    try:
        jobs = st.list_migration_jobs(wave_id=wave_id, status=status)
        t = Table("job_id", "server_id", "wave", "kind", "status", "executor_ref", "gates")
        for j in jobs:
            t.add_row(j.id, j.server_id, j.wave_id, j.kind, j.status,
                      j.executor_ref or "-", str(len(j.validation_gates)))
        console.print(t if jobs else "[dim]no migration jobs[/dim]")
    finally:
        st.close()


@migrate_cli.command("advance")
def migrate_advance(job_id: str = typer.Argument(...),
                     to: str = typer.Option(..., "--to", help="target status"),
                     note: str = typer.Option("", "--note")):
    """Advance a migration job to the next status (validated against the state machine)."""
    from ..core.execute import advance, InvalidTransition, GatesNotSatisfied
    st = _store()
    try:
        j = st.get_migration_job(job_id)
        if not j:
            console.print(f"[red]no such job {job_id}[/red]"); raise typer.Exit(1)
        try:
            advance(j, to, store=st, by="cli", note=note)
        except InvalidTransition as e:
            console.print(f"[red]{e}[/red]"); raise typer.Exit(1)
        except GatesNotSatisfied as e:
            console.print(f"[red]{e}[/red]"); raise typer.Exit(1)
        console.print(f"[green]{job_id}: {j.status}[/green]")
    finally:
        st.close()


@migrate_cli.command("revert")
def migrate_revert(job_id: str = typer.Argument(...),
                   to: str = typer.Option(..., "--to", help="target status")):
    """Revert a migration job to an earlier status."""
    from ..core.execute import revert, InvalidTransition
    st = _store()
    try:
        j = st.get_migration_job(job_id)
        if not j:
            console.print(f"[red]no such job {job_id}[/red]"); raise typer.Exit(1)
        try:
            revert(j, to, store=st, by="cli")
        except InvalidTransition as e:
            console.print(f"[red]{e}[/red]"); raise typer.Exit(1)
        console.print(f"[green]{job_id}: {j.status}[/green]")
    finally:
        st.close()


@migrate_cli.command("validate")
def migrate_validate(job_id: str = typer.Argument(...)):
    """Run the job's validation gates and report results."""
    from ..core.execute import run_validation_gates
    st = _store()
    try:
        j = st.get_migration_job(job_id)
        if not j:
            console.print(f"[red]no such job {job_id}[/red]"); raise typer.Exit(1)
        ok, _ = run_validation_gates(j, get_settings())
        st.upsert_migration_job(j)
        t = Table("gate", "kind", "must_pass", "result", "detail")
        for g in j.validation_gates:
            t.add_row(g.get("name", "-"), g.get("kind", "-"),
                      "yes" if g.get("must_pass") else "-",
                      g.get("result", "-"), g.get("detail", "-"))
        console.print(t)
        console.print(f"[{'green' if ok else 'red'}]must_pass gates {'OK' if ok else 'NOT satisfied'}[/]")
    finally:
        st.close()


@migrate_cli.command("complete")
def migrate_complete(job_id: str = typer.Argument(...),
                     note: str = typer.Option("", "--note")):
    """Manual mark-complete: operator confirms migration done + gates satisfied."""
    from ..core.execute import complete, InvalidTransition
    st = _store()
    try:
        j = st.get_migration_job(job_id)
        if not j:
            console.print(f"[red]no such job {job_id}[/red]"); raise typer.Exit(1)
        try:
            complete(j, store=st, by="cli", note=note)
        except InvalidTransition as e:
            console.print(f"[red]{e}[/red]"); raise typer.Exit(1)
        console.print(f"[green]{job_id}: {j.status}[/green]")
    finally:
        st.close()


def main():
    app_cli()


if __name__ == "__main__":
    main()