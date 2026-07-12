"""Data-gap report — traditional-IDC information-blindspot scorecard.

Traditional IDCs run on CMDBs that are incomplete, stale, or wrong: 脱保 hardware
with no support date, shadow IT the CMDB never knew about, hosts with no role /
no utilization telemetry / no code profile. The F4 ``assessment_confidence``
score already encodes "how much do we know about THIS host"; this module rolls
those per-host signals up to a *portfolio* view so the operator sees the blind
spots in one place and can decide what to fix before planning cutover.

Pure w.r.t. the store: reads servers + code profiles, returns a JSON-serializable
dict. No LLM, no writes. Used by the backend ``/api/data-gaps`` endpoint, the
``idc data-gaps`` CLI command, and the web "Data Quality" tab. The shadow-IT
discovery diff (Gap1) is a separate module (``ingest.discovery``) surfaced on
the same tab.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .match import _KEY_FIELDS, warranty_bucket, sizing_basis_of
from .models import Server
from .eol import os_eol_bucket

# confidence buckets for the distribution (parallel to the readiness/dashboard ones)
CONF_HIGH = "high(>=0.85)"
CONF_MED = "medium(0.5-0.85)"
CONF_LOW = "low(<0.5)"
CONF_NONE = "none"


def _conf_bucket(v) -> str:
    if v is None:
        return CONF_NONE
    if v >= 0.85:
        return CONF_HIGH
    if v >= 0.5:
        return CONF_MED
    return CONF_LOW


def _missing_key_fields(s: Server) -> List[str]:
    """Which characterization fields (F4 _KEY_FIELDS) are empty on this host."""
    out = []
    for f in _KEY_FIELDS:
        v = getattr(s, f, None)
        if f in ("cpu_cores", "mem_gb"):
            if not v:  # 0 / None -> missing
                out.append(f)
        elif not v:
            out.append(f)
    return out


def portfolio_data_gaps(store, today_iso: str = "", top: int = 12) -> Dict[str, Any]:
    """Portfolio data-quality / information-blindspot report.

    Returns:
      * ``total_servers``           — estate size.
      * ``confidence``              — assessment_confidence distribution
                                      (high/medium/low/none).
      * ``missing``                 — ``{field: count}`` for each F4 key field
                                      across the estate.
      * ``missing_utilization``     — hosts with no live utilization telemetry
                                      (``sizing_basis != measured``).
      * ``missing_code_profile``    — hosts that have apps but none of their
                                      apps have an executor code profile.
      * ``warranty``                — ``{active/expiring/expired/unknown}``
                                      distribution from ``match.warranty_bucket``.
      * ``os_eol``                  — same distribution for the OS vendor support
                                      status from ``eol.os_eol_bucket`` (the
                                      silent-rehost-of-CentOS-6 blindspot).
      * ``worst_hosts``             — top-N most thinly-known hosts (lowest
                                      confidence first), each with its missing
                                      fields + warranty + os_eol bucket, so the
                                      operator knows where to focus discovery.
      * ``summary``                 — one-line human summary.
    """
    servers: List[Server] = store.list_all_servers()
    profiles = store.list_code_profiles()
    profiled_apps = {p.app_id for p in profiles}

    total = len(servers)
    conf_dist = {CONF_HIGH: 0, CONF_MED: 0, CONF_LOW: 0, CONF_NONE: 0}
    missing: Dict[str, int] = {f: 0 for f in _KEY_FIELDS}
    missing_util = 0
    missing_profile = 0
    warranty = {"active": 0, "expiring": 0, "expired": 0, "unknown": 0}
    os_eol = {"active": 0, "expiring": 0, "expired": 0, "unknown": 0}

    per_host: List[Dict[str, Any]] = []
    for s in servers:
        conf_dist[_conf_bucket(s.assessment_confidence)] += 1
        mf = _missing_key_fields(s)
        for f in mf:
            missing[f] += 1
        if sizing_basis_of(s) != "measured":
            missing_util += 1
        if s.app_ids and not any(a in profiled_apps for a in s.app_ids):
            missing_profile += 1
        wb = warranty_bucket(s, today_iso)
        warranty[wb] = warranty.get(wb, 0) + 1
        ob = os_eol_bucket(s, today_iso)
        os_eol[ob] = os_eol.get(ob, 0) + 1
        per_host.append({
            "server_id": s.id, "hostname": s.hostname,
            "confidence": s.assessment_confidence,
            "missing": mf, "missing_count": len(mf),
            "has_util": sizing_basis_of(s) == "measured",
            "has_profile": bool(s.app_ids and any(a in profiled_apps for a in s.app_ids)),
            "warranty": wb, "os_eol": ob,
        })

    # worst hosts: lowest *scored* confidence first, then most missing fields.
    # None-confidence hosts (not rebuilt yet) sort LAST — they need a rebuild,
    # not discovery, so the actionable thinly-known hosts surface ahead of them.
    per_host.sort(key=lambda h: (
        h["confidence"] if h["confidence"] is not None else 2.0,
        -h["missing_count"]))
    worst = per_host[:top]

    characterized = sum(1 for s in servers if (s.assessment_confidence or 0) >= 0.5)
    pct = round(100.0 * characterized / total, 1) if total else 0.0
    total_gaps = (sum(missing.values()) + missing_util + missing_profile
                  + warranty.get("unknown", 0))
    summary = (f"{total} hosts · {pct}% characterized (conf≥0.5) · "
               f"{warranty.get('unknown',0)} unknown warranty · "
               f"{os_eol.get('expired',0)} OS EOL · "
               f"{missing_util} no util telemetry · {total_gaps} total gaps")

    return {
        "total_servers": total,
        "confidence": conf_dist,
        "missing": missing,
        "missing_utilization": missing_util,
        "missing_code_profile": missing_profile,
        "warranty": warranty,
        "os_eol": os_eol,
        "worst_hosts": worst,
        "summary": summary,
    }


__all__ = ["portfolio_data_gaps", "CONF_HIGH", "CONF_MED", "CONF_LOW", "CONF_NONE"]