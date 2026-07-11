"""Prometheus adapter.

Online: run a small set of PromQL instant queries (cpu/mem/disk/net usage)
via ``/api/v1/query`` and attach the per-instance results to RawAssets.
Offline: fixture JSON in the same instant-vector shape.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ...config import FIXTURES, Settings
from ..models import RawAsset, SOURCE_PROMETHEUS
from .base import Adapter, IngestResult, register


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
        return self._fixture()

    def _fixture(self) -> IngestResult:
        path = FIXTURES / "prometheus_metrics.json"
        data = json.loads(path.read_text(encoding="utf-8"))
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
            assets.append(RawAsset(
                source=SOURCE_PROMETHEUS, source_id=host,
                hostname=host, fqdn="", ip="",
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
        assets = [RawAsset(source=SOURCE_PROMETHEUS, source_id=h, hostname=h, fqdn="", ip="",
                           attrs={"utilization": u}) for h, u in by_host.items()]
        return IngestResult(assets=assets, mode="online",
                            error="; ".join(errors) if errors else "")