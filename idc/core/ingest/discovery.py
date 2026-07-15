"""Shadow-IT / CMDB-drift discovery (data-gap — traditional IDC).

NOT a standard ingest adapter: produces no ``RawAsset`` rows and is not
registered in the ingest registry. Like ``ingest.netdep`` / ``ingest.warranty``
it consumes already-resolved ``Server`` records and compares them against an
external "what's actually on the network / in the hypervisor" snapshot to
surface the three traditional-IDC blindspots:

  * ``unknown_hosts``  — seen in the discovery source but NOT in CMDB (shadow
                          IT: a box someone racked/VMed without a ticket).
  * ``cmdb_orphans``   — in CMDB but NOT seen in the discovery source (zombie
                          / already-retired / decommissioned candidate).
  * ``drifted``        — in both, but a characterization attribute (role/os)
                          differs — CMDB is stale.

Source (``IDC_DISCOVERY_PATH``, default empty = OFF): a JSON list of records

    [{"hostname": "app-orders-01", "ips": ["10.0.1.5"], "source": "vcenter",
      "role": "web", "os": "centos"}, ...]

Matching is by hostname OR fqdn OR any ip (lowercased) so it works regardless of
which key the discovery source emits. Off by default (empty path -> empty diff)
so the system runs out-of-the-box with zero new credentials. No LLM; the diff is
deterministic and reproducible.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..models import Server, _now


def _read_records(path: str) -> List[Dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _as_list(v: Any) -> List[Any]:
    """Coerce a discovery field to a list. Some sources emit a scalar (e.g.
    ``"ips": "10.0.1.5"``) where a list is expected; iterating the bare string
    would yield single-char keys. Wrap scalars, pass lists through, drop None."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _keys(r: Dict[str, Any]) -> List[str]:
    """All lowercased identity keys for a discovery record (hostname/fqdn/ips)."""
    out: List[str] = []
    for k in ("hostname", "fqdn"):
        v = (r.get(k) or "").strip().lower()
        if v:
            out.append(v)
    for ip in _as_list(r.get("ips")):
        ip = str(ip).strip().lower()
        if ip:
            out.append(ip)
    return out


def _server_keys(s: Server) -> List[str]:
    out: List[str] = []
    for k in (s.hostname, s.fqdn):
        if k:
            out.append(k.lower().strip())
    for ip in (s.ips or []):
        if ip:
            out.append(str(ip).lower().strip())
    return out


def discover_drift(settings, servers: List[Server]) -> Dict[str, Any]:
    """Compare a discovery snapshot against the CMDB servers (Gap1).

    Returns ``{unknown_hosts, cmdb_orphans, drifted, source, scanned_at,
    summary}``. Empty diff when ``IDC_DISCOVERY_PATH`` is unset (the default
    off state). Pure w.r.t. the snapshot + the server list; no DB, no LLM.
    """
    path = (settings.discovery_path or "").strip()
    if not path:
        return {"unknown_hosts": [], "cmdb_orphans": [], "drifted": [],
                "source": "off", "scanned_at": _now(),
                "summary": "discovery off (IDC_DISCOVERY_PATH unset)"}

    records = _read_records(path)
    # index CMDB servers by every identity key (hostname/fqdn/ip, lowercased)
    cmdb_by_key: Dict[str, Server] = {}
    for s in servers:
        for k in _server_keys(s):
            cmdb_by_key.setdefault(k, s)
    # index discovery records the same way
    disc_by_key: Dict[str, Dict[str, Any]] = {}
    for r in records:
        for k in _keys(r):
            disc_by_key.setdefault(k, r)

    unknown: List[Dict[str, Any]] = []
    drifted: List[Dict[str, Any]] = []
    for r in records:
        match_s: Server | None = None
        for k in _keys(r):
            s = cmdb_by_key.get(k)
            if s:
                match_s = s
                break
        if match_s is None:
            unknown.append({
                "hostname": r.get("hostname") or r.get("fqdn") or "",
                "fqdn": r.get("fqdn") or "", "ips": _as_list(r.get("ips")),
                "source": r.get("source") or "discovery",
                "role": r.get("role") or "", "os": r.get("os") or "",
                "reason": "no CMDB match (hostname/fqdn/ip)",
            })
            continue
        # in both — check characterization drift on role/os
        for f in ("role", "os"):
            dv = (r.get(f) or "").strip().lower()
            sv = (getattr(match_s, f, "") or "").strip().lower()
            if dv and sv and dv != sv:
                drifted.append({"hostname": match_s.hostname, "field": f,
                                "cmdb": sv, "discovered": dv})

    cmdb_orphans: List[Dict[str, Any]] = []
    for s in servers:
        keys = _server_keys(s)
        if not any(k in disc_by_key for k in keys):
            cmdb_orphans.append({
                "hostname": s.hostname, "fqdn": s.fqdn, "ips": list(s.ips or []),
                "role": s.role or "", "source": "cmdb",
                "reason": "not seen in discovery scan",
            })

    source = (records[0].get("source") if records else "discovery") or "discovery"
    summary = (f"{len(unknown)} unknown (shadow IT) / "
               f"{len(cmdb_orphans)} orphans (zombie/retired) / "
               f"{len(drifted)} drifted")
    return {"unknown_hosts": unknown, "cmdb_orphans": cmdb_orphans,
            "drifted": drifted, "source": source, "scanned_at": _now(),
            "summary": summary}


__all__ = ["discover_drift"]