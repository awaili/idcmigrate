"""Hardware warranty / end-of-support fold-in (data-gap — traditional IDC).

NOT a standard ingest adapter: produces no ``RawAsset`` rows and is not
registered in the ingest registry. Like ``ingest.netdep`` it consumes already-
resolved ``Server`` records and merges hardware-support fields onto them so
the F2 on-prem extended-support premium, the F10 ``hw_support`` readiness
signal, and the data-gaps "unknown warranty" count have data to work with.

Source (``IDC_WARRANTY_PATH``, default empty = OFF): a JSON list of records

    [{"hostname": "db-prod-01", "hardware_eol": "2024-03-01",
      "warranty_status": "expired"}, ...]

Matching is by hostname OR fqdn (lowercased), so this works whether the
procurement/asset system keys on the short name or the FQDN. ``warranty_status``
is optional — when omitted, ``match.warranty_bucket`` derives the bucket from
``hardware_eol`` at read time. Off by default (empty path → no-op, returns a
zero report) so the system runs out-of-the-box with zero new credentials.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..models import Server


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


def merge_warranty(servers: List[Server], path: str) -> Dict[str, Any]:
    """Merge hardware-support records onto matching servers in place.

    Returns a small report ``{matched, records, unmatched}`` where
    ``unmatched`` is the list of record hostnames that found no server (a data
    gap in the other direction — warranty data for a host the CMDB doesn't
    know, i.e. a likely shadow-IT / decommissioned candidate). No-op when
    ``path`` is empty (the default off state).
    """
    records = _read_records(path)
    if not records:
        return {"matched": 0, "records": 0, "unmatched": []}

    # hostname / fqdn (lowercased) -> server, for matching warranty records
    by_host: Dict[str, Server] = {}
    for s in servers:
        for k in (s.hostname, s.fqdn):
            if k:
                by_host[k.lower().strip()] = s

    matched = 0
    unmatched: List[str] = []
    for r in records:
        host = (r.get("hostname") or r.get("fqdn") or "").strip().lower()
        if not host:
            continue
        s = by_host.get(host)
        if not s:
            unmatched.append(host)
            continue
        eol = (r.get("hardware_eol") or "").strip()
        ws = (r.get("warranty_status") or "").strip().lower()
        if eol:
            s.hardware_eol = eol
        if ws:
            s.warranty_status = ws
        matched += 1
    return {"matched": matched, "records": len(records), "unmatched": unmatched}


__all__ = ["merge_warranty"]