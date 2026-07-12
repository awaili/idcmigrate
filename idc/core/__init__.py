"""Core pipeline: ingest → normalize → match → plan.

``run_ingest`` pulls raw assets from one or all sources into the store.
``rebuild`` normalizes all raw assets into Server records (replacing prior),
runs matching, loads+resolves workloads, and builds the wave plan — the single
entrypoint used by both CLI and backend to refresh derived data.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Dict, List, Optional

from ..config import Settings, get_settings
from .codeintel import enrich_match, enrich_match_db, index_profiles
from .cost import estimate_server, load_pricebook, strategies_from_tags
from .models import (
    IngestRun, RawAsset, _now,
    SOURCE_PROMETHEUS, SOURCE_RVTOOLS, SOURCE_SERVICENOW, SOURCE_ZABBIX,
)
from .db import Store, open_store
from .eol import apply_os_eol, os_eol_bucket
from .ingest import all_sources, get_adapter
from .ingest.netdep import discover_network_deps
from .ingest.warranty import merge_warranty
from .match import assessment_confidence_of, match_servers, sizing_basis_of, warranty_bucket
from .normalize import load_workloads, merge_network_deps, normalize, resolve_workloads
from .plan import plan_waves


# Per-source offline override map: (path field, [API-cred fields to clear]).
# Clearing the cred fields forces the adapter's has_*() check to be False so
# it reads the file even when live API creds are configured in the env.
_OFFLINE_CLEAR = {
    SOURCE_SERVICENOW: ("sn_path", ["sn_base", "sn_token", "sn_user", "sn_password"]),
    SOURCE_RVTOOLS:    ("rvtools_path", []),
    SOURCE_ZABBIX:     ("zbx_path", ["zbx_url", "zbx_token", "zbx_user", "zbx_password"]),
    SOURCE_PROMETHEUS: ("prom_path", ["prom_url"]),
}


def _offline_settings(settings: Settings, src: str, path: Optional[str]) -> Settings:
    """Return a settings copy forced into offline/file mode for ``src``.

    If ``path`` is given, that source's file path is pointed at it (used by the
    web upload endpoint to ingest an uploaded file in-place).
    """
    path_field, cred_fields = _OFFLINE_CLEAR[src]
    changes: Dict[str, object] = {f: "" for f in cred_fields}
    if path:
        changes[path_field] = path
    return dataclasses.replace(settings, **changes)


def run_ingest(store: Store, settings: Settings, source: str = "all",
               force_offline: bool = False,
               path_overrides: Optional[Dict[str, str]] = None) -> List[IngestRun]:
    """Pull raw assets into the store.

    ``force_offline`` ignores live API creds and reads each source's file
    (fixture or configured ``IDC_*_PATH``). ``path_overrides`` maps a source
    name → file path to ingest an ad-hoc file for just that source (e.g. a web
    upload); the override implies offline for that source.
    """
    path_overrides = path_overrides or {}
    runs: List[IngestRun] = []
    sources = all_sources() if source == "all" else [source]
    for src in sources:
        ad = get_adapter(src)
        s = settings
        if force_offline or src in path_overrides:
            s = _offline_settings(settings, src, path_overrides.get(src))
        res = ad.fetch(s)
        store.upsert_raw(res.assets)
        run = IngestRun(source=src, mode=res.mode, raw_count=len(res.assets),
                        finished_at=_now(), error=res.error)
        store.record_ingest(run)
        runs.append(run)
    return runs


def rebuild(store: Store, settings: Optional[Settings] = None,
            do_match: bool = True, do_plan: bool = True,
            max_waves: int = 0,
            on_progress: Optional[Callable[[str, dict], None]] = None) -> dict:
    """Rebuild servers/matches/waves from stored raw assets. Returns stats.

    ``max_waves`` — when >0, a hard cap on the total wave count (see
    ``plan_waves``); unifies the cap guarantee with the LLM planner path.

    ``on_progress`` — optional ``on_progress(phase, payload)`` hook called at
    each stage so the HTTP layer can stream live progress (the UI's /api/rebuild
    consumes it as NDJSON). CLI/tests pass None. The work itself is unchanged."""
    settings = settings or get_settings()

    def _prog(stage: str, **payload) -> None:
        if on_progress is not None:
            try:
                on_progress(stage, payload)
            except Exception:
                pass   # progress reporting must never break the rebuild

    rows = store.list_raw()
    assets = [RawAsset(source=r["source"], source_id=r["source_id"],
                       hostname=r["hostname"] or "", fqdn=r["fqdn"] or "",
                       ip=r["ip"] or "", attrs=__import__("json").loads(r["attrs"] or "{}"),
                       ingested_at=r["ingested_at"] or "")
              for r in rows]
    servers = normalize(assets)
    # code profiles from the external agent executor (reconsolidate signal).
    # Loaded early so F4 assessment-confidence can credit servers whose app has
    # a code profile before servers are persisted.
    profiles = store.list_code_profiles()
    pidx = index_profiles(profiles)
    srv_by_id: Dict[str, "Server"] = {s.id: s for s in servers}
    # workloads — resolved early so Server.app_ids can be back-populated from
    # the workload->server mapping (the app assignment lives in apps.csv, not
    # ServiceNow). F4 (has_code_profile), F1 netdep (app-level edges) and plan
    # all read Server.app_ids, so it must be populated before they run.
    wls = resolve_workloads(load_workloads(), servers)
    for w in wls:
        for sid in w.server_ids:
            srv = srv_by_id.get(sid)
            if srv and w.app_id not in srv.app_ids:
                srv.app_ids.append(w.app_id)
    # data-gap — fold hardware warranty / end-of-support onto matching servers
    # (off by default; IDC_WARRANTY_PATH empty -> no-op). Runs BEFORE the
    # warranty_bucket computation so the persisted bucket reflects the merged
    # warranty_status/hardware_eol (otherwise the facet is stale "unknown" for
    # every merged host).
    warranty_report = merge_warranty(servers, settings.warranty_path)
    # F4 — set sizing basis (measured/estimated) + data-coverage confidence on
    # each server, so the persisted row carries the signals the UI renders and
    # enrich_match can scale the base rule confidence by coverage.
    for s in servers:
        s.sizing_basis = sizing_basis_of(s)
        s.assessment_confidence = assessment_confidence_of(
            s, has_code_profile=any(a in pidx for a in s.app_ids))
        # data-gap — persist the derived support buckets so the inventory can
        # facet/filter on them (merge_warranty above filled the raw fields).
        s.warranty_bucket = warranty_bucket(s)
        s.os_eol_bucket = os_eol_bucket(s)
    # F1 — fold runtime network-dependency edges into Workload.depends_on +
    # src-server provenance. Runs after app_ids back-population so edges can
    # resolve to app level. Done BEFORE the (single) server write so the netdep
    # source_refs land in that write — no second pass over the estate.
    netdep_report: Dict = {}
    if settings.netdep_enabled():
        netdeps = discover_network_deps(settings, servers)
        wls, netdep_report = merge_network_deps(servers, wls, netdeps)
    _prog("normalize", servers=len(servers), workloads=len(wls),
          netdep=bool(netdep_report), warranty=bool(warranty_report.get("matched")))

    # --- persistence ----------------------------------------------------------
    # All enrichment above is in-memory; the writes below are the only DB cost.
    # Each replace_* is one transaction (DELETE + chunked multi-row INSERT) —
    # previously this section did ~3×N per-row transactions (the 2-min hotspot
    # for a 15K estate). servers/workloads/matches+costs/waves = 4 transactions.
    _prog("persist:servers")
    store.replace_servers(servers)
    _prog("persist:workloads")
    store.replace_workloads(wls)

    # matches — enriched with code-intel (readiness/blockers/pattern) AND scaled
    # by data-coverage confidence (F4). F2 — per-server run cost estimated from
    # the matched target + price book and persisted to the cost_estimates side
    # table; the hydrated Match.cost is not persisted (side table is the truth).
    # F5 — DB-host matches are further enriched with the heterogeneous DB
    # conversion profile (difficulty grade → confidence dip + replatform force).
    matches: List = []
    costs: List = []
    if do_match:
        book = load_pricebook(settings)
        # F5 — db_profiles are keyed by the DB host's STABLE identity (hostname),
        # not the transient Server.id (ids are fresh each rebuild). Index by the
        # stored db_server_id and resolve via the server's hostname/identity.
        dbidx = {d.db_server_id: d for d in store.list_db_profiles()}
        matched = match_servers(servers)
        for m in matched:
            profile = _profile_for_server(m.server_id, servers, pidx)
            srv = srv_by_id.get(m.server_id)
            # data-gap — flag an out-of-support OS before enrichment so the EOL
            # confidence dip compounds with the F4 coverage scale (no silent
            # rehost of CentOS 6 / Windows 2008). No-op for active/unknown OS.
            if srv:
                apply_os_eol(m, srv)
            cov = srv.assessment_confidence if srv else None
            enrich_match(m, profile, assessment_confidence=cov or 1.0)
            dbp = None
            if srv:
                for k in (srv.hostname.lower().strip(), srv.identity_key, srv.id):
                    dbp = dbidx.get(k)
                    if dbp:
                        break
            enrich_match_db(m, dbp)
            if srv:
                m.cost = estimate_server(srv, m, book)
                costs.append(m.cost)
            matches.append(m)
        _prog("match", total=len(matched))
    _prog("persist:matches", matches=len(matches), costs=len(costs))
    store.replace_matches_and_costs(matches, costs)

    # waves — code deps merged in + effort/pattern ordering + rationale.
    # strategies = AI-assigned 7R overlay (app_strategies); retain/retire apps
    # are excluded from migration waves. Loaded here so a Rebuild honors 7R
    # assignments the operator persisted via the 7R UI / CLI.
    waves: List = []
    if do_plan:
        strategies = {s.app_id: s for s in store.list_app_strategies()}
        # F2 — operator tag-driven retain/retire overrides win over AI strategy
        for app_id, strat in strategies_from_tags(servers).items():
            strategies[app_id] = strat
        waves = plan_waves(servers, wls, code_profiles=profiles,
                          max_waves=max_waves, strategies=strategies,
                          min_assessment_confidence=settings.min_assessment_confidence)
        _prog("plan", waves=len(waves))
    # replace_waves always runs (also when do_plan=False) so the waves table is
    # wiped along with the other derived tables — matches the old _wipe_derived
    # contract that no stale wave rows survive a rebuild.
    _prog("persist:waves", waves=len(waves))
    store.replace_waves(waves)
    out = store.stats()
    if netdep_report:
        out["netdep"] = netdep_report
    if warranty_report and warranty_report["matched"]:
        out["warranty"] = warranty_report
    return out


def _profile_for_server(server_id, servers, pidx):
    """Pick the first CodeProfile matching one of the server's app_ids."""
    s = next((x for x in servers if x.id == server_id), None)
    if not s:
        return None
    for a in s.app_ids:
        if a in pidx:
            return pidx[a]
    return None


__all__ = [
    "Store", "open_store", "run_ingest", "rebuild",
    "normalize", "match_servers", "plan_waves", "load_workloads", "resolve_workloads",
    "warranty_bucket",
]