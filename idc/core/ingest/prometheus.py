"""Prometheus adapter.

Online: run a small set of PromQL instant queries (cpu/mem/disk/net usage)
via ``/api/v1/query`` and attach the per-instance results to RawAssets.
Offline: fixture JSON in the same instant-vector shape.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ...config import Settings
from ..models import RawAsset, SOURCE_PROMETHEUS
from .base import Adapter, IngestResult, register


def _split_instance(inst: str) -> tuple:
    """Prometheus ``instance`` labels are ``host:port`` (or ``host``).

    Strip the trailing ``:port`` so the asset's hostname/IP matches the
    other sources' identity tokens — otherwise ``10.0.0.1:9100`` never
    merged with the CMDB/RVTools host and created a phantom server that
    hoarded the utilization data. Returns ``(host, ip_if_host_is_an_ip)``.
    """
    inst = (inst or "").strip()
    # only strip a trailing :<digits>; leave IPv6 / non-port colons alone
    if ":" in inst and not inst.startswith("["):
        host, _, port = inst.rpartition(":")
        if port.isdigit():
            inst = host
    # treat a bare IP as both hostname and ip so union-find joins it
    ip = ""
    parts = inst.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        ip = inst
    return inst, ip


# (key, promql) — keyed to the utilization field we want to fill
QUERIES = {
    "cpu_p95": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "mem_p95": '(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100',
    "disk_used_pct": '100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"} * 100)',
    "net_rx_mbps": 'rate(node_network_receive_bytes_total{device!~"lo"}[5m]) * 8 / 1e6',
    "net_tx_mbps": 'rate(node_network_transmit_bytes_total{device!~"lo"}[5m]) * 8 / 1e6',
}


@register
class PrometheusAdapter(Adapter):
    source = SOURCE_PROMETHEUS

    def fetch(self, settings: Settings) -> IngestResult:
        if settings.has_prometheus():
            return self._online(settings)
        if not settings.allow_fixture_fallback:
            return self._fixture_disabled(settings)
        return self._fixture(settings)

    def _fixture(self, settings: Settings) -> IngestResult:
        path = settings.prom_path
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as e:
            return IngestResult(assets=[], mode="fixture",
                                error=f"prometheus file not found: {path} ({e!r})")
        result = data.get("data", {}).get("result", {})
        # group by instance across all metrics
        by_host: Dict[str, Dict[str, float]] = {}
        for metric, samples in result.items():
            for s in samples:
                inst = (s.get("metric") or {}).get("instance", "")
                try:
                    val = float(s.get("value", [0, "0"])[1])
                except (ValueError, TypeError, IndexError):
                    continue
                by_host.setdefault(inst, {})[metric] = val
        assets: List[RawAsset] = []
        for host, util in by_host.items():
            hostname, ip = _split_instance(host)
            assets.append(RawAsset(
                source=SOURCE_PROMETHEUS, source_id=hostname,
                hostname=hostname, fqdn="", ip=ip,
                attrs={"utilization": util},
            ))
        return IngestResult(assets=assets, mode="fixture")

    def _online(self, settings: Settings) -> IngestResult:
        import httpx

        base = settings.prom_url.rstrip("/")
        by_host: Dict[str, Dict[str, float]] = {}
        errors: List[str] = []
        for key, query in QUERIES.items():
            try:
                r = httpx.get(f"{base}/api/v1/query", params={"query": query},
                              timeout=settings.prom_timeout)
                r.raise_for_status()
                j = r.json()
                if j.get("status") != "success":
                    errors.append(f"{key}: {j.get('errorType')} {j.get('error')}")
                    continue
                for s in j.get("data", {}).get("result", []):
                    inst = (s.get("metric") or {}).get("instance", "")
                    try:
                        val = float(s.get("value", [0, "0"])[1])
                    except (ValueError, TypeError, IndexError):
                        continue
                    by_host.setdefault(inst, {})[key] = val
            except Exception as e:
                errors.append(f"{key}: {e!r}")
        assets = []
        for h, u in by_host.items():
            hostname, ip = _split_instance(h)
            assets.append(RawAsset(source=SOURCE_PROMETHEUS, source_id=hostname,
                                   hostname=hostname, fqdn="", ip=ip,
                                   attrs={"utilization": u}))
        return IngestResult(assets=assets, mode="online",
                            error="; ".join(errors) if errors else "")