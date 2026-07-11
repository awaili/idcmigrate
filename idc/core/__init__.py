"""Core pipeline: ingest → normalize → match → plan.

``run_ingest`` pulls raw assets from one or all sources into the store.
``rebuild`` normalizes all raw assets into Server records (replacing prior),
runs matching, loads+resolves workloads, and builds the wave plan — the single
entrypoint used by both CLI and backend to refresh derived data.
"""
from __future__ import annotations

from typing import List, Optional

from ..config import Settings, get_settings
from .models import IngestRun, RawAsset, _now
from .db import Store, open_store
from .ingest import all_sources, get_adapter
from .match import match_servers
from .normalize import load_workloads, normalize, resolve_workloads
from .plan import plan_waves


def run_ingest(store: Store, settings: Settings, source: str = "all") -> List[IngestRun]:
    runs: List[IngestRun] = []
    sources = all_sources() if source == "all" else [source]
    for src in sources:
        ad = get_adapter(src)
        res = ad.fetch(settings)
        store.upsert_raw(res.assets)
        run = IngestRun(source=src, mode=res.mode, raw_count=len(res.assets),
                        finished_at=_now(), error=res.error)
        store.record_ingest(run)
        runs.append(run)
    return runs


def rebuild(store: Store, settings: Optional[Settings] = None,
            do_match: bool = True, do_plan: bool = True) -> dict:
    """Rebuild servers/matches/waves from stored raw assets. Returns stats."""
    settings = settings or get_settings()
    rows = store.list_raw()
    assets = [RawAsset(source=r["source"], source_id=r["source_id"],
                       hostname=r["hostname"] or "", fqdn=r["fqdn"] or "",
                       ip=r["ip"] or "", attrs=__import__("json").loads(r["attrs"] or "{}"),
                       ingested_at=r["ingested_at"] or "")
              for r in rows]
    servers = normalize(assets)
    # clear+rewrite servers: simplest is upsert (ids are fresh each rebuild).
    # To avoid dup growth we wipe the servers table first.
    _wipe_derived(store)
    for s in servers:
        store.upsert_server(s)
    # workloads
    wls = resolve_workloads(load_workloads(), servers)
    for w in wls:
        store.upsert_workload(w)
    # attach app_ids onto servers (so plan can use them) — already on servers
    # matches
    if do_match:
        for m in match_servers(servers):
            store.upsert_match(m)
    # waves
    if do_plan:
        waves = plan_waves(servers, wls)
        for w in waves:
            store.upsert_wave(w)
    return store.stats()


def _wipe_derived(store: Store) -> None:
    with store.tx() as cur:
        cur.execute("DELETE FROM servers")
        cur.execute("DELETE FROM matches")
        cur.execute("DELETE FROM waves")
        cur.execute("DELETE FROM workloads")


__all__ = [
    "Store", "open_store", "run_ingest", "rebuild",
    "normalize", "match_servers", "plan_waves", "load_workloads", "resolve_workloads",
]