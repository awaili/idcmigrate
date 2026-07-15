"""F1 — network dependency discovery.

Discovers runtime network edges (src host -> dst ip:port) from telemetry and
returns them as ``NetDep`` records. NOT a standard ingest adapter: it does not
produce ``RawAsset`` rows and is not registered in the ingest registry. It
consumes already-resolved ``Server`` records (to map telemetry hosts to
server_ids) and emits dependency edges that ``normalize.merge_network_deps``
folds into ``Workload.depends_on``.

Sources (``IDC_NETDEP_SOURCE``):
  * ``collector`` / ``fixture`` (default) — read a JSON snapshot of
    agent-collected ``ss -tn``-style output (``fixtures/netdep.json`` or
    ``IDC_NETDEP_PATH``). This is the realistic primary source: standard
    Prometheus/Zabbix exporters do NOT expose per-connection dst ip:port, so a
    small agent (or eBPF / conntrack exporter) is required for live data.
  * ``prometheus`` — query a custom connection-exporter metric; falls back to
    the fixture on error/empty (standard node_exporter has no per-connection
    metric, so this needs a custom exporter).
  * ``zabbix`` — ``item.get`` / ``history.get`` for a custom item key; falls
    back to fixture on error/empty.
  * ``off`` — disabled (returns []).

The fixture fallback means the system runs out-of-the-box like the other
sources, with zero credentials.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ...config import Settings
from ..models import NetDep, Server, _now


def _host_to_server_id(servers: List[Server]) -> Dict[str, str]:
    """hostname / fqdn (lowercased) -> server_id, for src resolution."""
    out: Dict[str, str] = {}
    for s in servers:
        for k in (s.hostname, s.fqdn):
            if k:
                out[k.lower()] = s.id
    return out


def _read_fixture(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _from_prometheus(settings: Settings, metric: str) -> List[Dict[str, Any]]:
    """Query a custom connection metric; returns [] on any failure so the
    caller falls back to the fixture.

    Expected metric: an instant vector whose series carry the dst as labels
    (``dst_ip``/``remote_ip`` + ``dst_port``/``remote_port``) and the src host
    as the ``instance`` label. Standard node_exporter does not expose this —
    a custom exporter (conntrack / eBPF / `ss` scraper) is required.
    """
    if not settings.has_prometheus() or not metric:
        return []
    try:
        import httpx
        r = httpx.get(f"{settings.prom_url}/api/v1/query",
                      params={"query": metric}, timeout=settings.prom_timeout)
        if r.status_code != 200:
            return []
        result = (r.json().get("data") or {}).get("result") or []
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for item in result:
        met = item.get("metric") or {}
        src = met.get("instance") or met.get("src_host") or ""
        if ":" in src and "@" not in src:
            src = src.rsplit(":", 1)[0]   # strip :port from instance label
        dst_ip = met.get("dst_ip") or met.get("remote_ip") or ""
        port = met.get("dst_port") or met.get("remote_port") or ""
        out.append({"src_host": src, "dst_ip": dst_ip,
                    "dst_port": int(port) if str(port).isdigit() else 0,
                    "observed_at": _now()})
    return out


def _from_zabbix(settings: Settings, item_key: str) -> List[Dict[str, Any]]:
    """Query a custom Zabbix item for connection records.

    Standard Zabbix items (``net.tcp.port[...]``) are active checks FROM the
    Zabbix server TO a target port — they do not capture a host's outbound
    edges. A custom trapper/item emitting JSON ``{dst_ip, dst_port}`` is
    required. This pulls the item's latest value per host via ``item.get`` +
    ``history.get`` and parses the JSON. Returns [] on any failure so the
    caller falls back to the fixture.
    """
    if not settings.has_zabbix() or not item_key:
        return []
    try:
        import httpx
        url = settings.zbx_url
        rid = 0

        def call(method: str, params: Dict[str, Any]) -> Any:
            nonlocal rid
            rid += 1
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
            if settings.zbx_token:
                payload["auth"] = settings.zbx_token
            r = httpx.post(url, json=payload, timeout=60)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j.get("result")

        # find the items matching the custom key across all hosts
        items = call("item.get", {
            "output": ["itemid", "hostid", "name"],
            "filter": {"key_": item_key},
            "selectHosts": ["host"],
        }) or []
        if not items:
            return []
        hostid_to_host = {it.get("hostid"): (it.get("hosts") or [{}])[0].get("host", "")
                         for it in items}
        # pull the latest value for each item
        rows: List[Dict[str, Any]] = []
        for it in items:
            hist = call("history.get", {
                "output": "value", "history": 2,  # 2 = text/char
                "itemids": it.get("itemid"), "sortfield": "clock",
                "sortorder": "DESC", "limit": 1,
            }) or []
            if not hist:
                continue
            try:
                val = json.loads(hist[0].get("value", ""))
            except Exception:
                continue   # not JSON -> skip this host
            src = hostid_to_host.get(it.get("hostid"), "")
            # the trapper value is one record OR a list of records
            for rec in (val if isinstance(val, list) else [val]):
                if not isinstance(rec, dict):
                    continue
                rows.append({
                    "src_host": src,
                    "dst_ip": rec.get("dst_ip") or rec.get("remote_ip") or "",
                    # a single non-numeric port ("any"/"N/A") would otherwise raise
                    # and the outer except aborts the WHOLE batch (silent []).
                    "dst_port": (lambda p: int(p) if str(p).isdigit() else 0)(
                        rec.get("dst_port") or rec.get("remote_port") or 0),
                    "observed_at": _now(),
                })
        return rows
    except Exception:
        return []


def discover_network_deps(settings: Settings, servers: List[Server]) -> List[NetDep]:
    """Discover runtime network dependency edges (F1).

    Returns ``NetDep`` records with ``src_server_id`` resolved (when the src
    host is known to the estate); dst resolution happens later in
    ``normalize.merge_network_deps``. Empty list when ``IDC_NETDEP_SOURCE=off``
    or no edges are found.
    """
    src = (settings.netdep_source or "").strip().lower()
    if src in ("", "off"):
        return []
    host_to_id = _host_to_server_id(servers)

    if src == "prometheus":
        rows = _from_prometheus(settings, settings.netdep_prom_metric)
    elif src == "zabbix":
        rows = _from_zabbix(settings, settings.netdep_zabbix_item)
    else:
        rows = _read_fixture(settings.netdep_path)   # collector / fixture

    # live source returned nothing -> fixture fallback (out-of-the-box safe)
    if not rows and src in ("prometheus", "zabbix"):
        rows = _read_fixture(settings.netdep_path)

    out: List[NetDep] = []
    edge_source = src if src in ("prometheus", "zabbix") else "collector"
    for r in rows:
        sh = (r.get("src_host") or "").strip()
        out.append(NetDep(
            src_server_id=host_to_id.get(sh.lower(), ""),
            src_host=sh,
            dst_ip=(r.get("dst_ip") or "").strip(),
            dst_port=int(r.get("dst_port") or 0),
            observed_at=r.get("observed_at") or _now(),
            source=edge_source,
        ))
    return out