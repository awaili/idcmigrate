"""Zabbix adapter.

Online: JSON-RPC ``host.get`` with tags + interfaces, then ``item.get`` /
``history.get`` for utilization (cpu/mem/disk/net). Offline: fixture JSON
that already carries per-host utilization so the pipeline is exercisable
without a live Zabbix.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ...config import Settings
from ..models import RawAsset, SOURCE_ZABBIX
from .base import Adapter, IngestResult, register


@register
class ZabbixAdapter(Adapter):
    source = SOURCE_ZABBIX

    def fetch(self, settings: Settings) -> IngestResult:
        if settings.has_zabbix():
            return self._online(settings)
        if not settings.allow_fixture_fallback:
            return self._fixture_disabled(settings)
        return self._fixture(settings)

    def _fixture(self, settings: Settings) -> IngestResult:
        path = settings.zbx_path
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as e:
            return IngestResult(assets=[], mode="fixture",
                                error=f"zabbix file not found: {path} ({e!r})")
        assets: List[RawAsset] = []
        for h in data.get("hosts", []):
            iface = (h.get("interfaces") or [{}])[0]
            util = h.get("utilization") or {}
            tags = {t.get("tag"): t.get("value") for t in h.get("tags", [])}
            assets.append(RawAsset(
                source=SOURCE_ZABBIX, source_id=str(h.get("hostid") or h.get("host")),
                hostname=h.get("host", ""), fqdn=(iface.get("dns") or ""),
                ip=iface.get("ip", ""),
                attrs={
                    "name": h.get("name"), "groups": [g.get("name") for g in h.get("groups", [])],
                    "tags": tags, "utilization": util,
                },
            ))
        return IngestResult(assets=assets, mode="fixture")

    # -- online ------------------------------------------------------------
    def _online(self, settings: Settings) -> IngestResult:
        import httpx

        url = settings.zbx_url
        rid = 1
        token = settings.zbx_token  # local copy — do NOT mutate shared Settings

        def call(method: str, params: Dict[str, Any]) -> Any:
            nonlocal rid, token
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
            rid += 1
            if token:
                payload["auth"] = token
            r = httpx.post(url, json=payload, timeout=60)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j.get("result")

        try:
            # token login only needed for user-based auth; token already set as auth
            if not token:
                token = call("user.login", {
                    "username": settings.zbx_user, "password": settings.zbx_password})
            hosts = call("host.get", {
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip", "dns", "type"],
                "selectGroups": ["name"],
                "selectTags": ["tag", "value"],
            })
        except Exception as e:
            if not settings.allow_fixture_fallback:
                # never pull dummy fixture data over a real (creds-configured)
                # live deployment on a transient API failure — that would inject
                # phantom hosts into the real inventory.
                return IngestResult(assets=[], mode="error",
                                    error=f"zabbix online failed ({e!r}); fixture fallback disabled")
            fix = self._fixture(settings)
            return IngestResult(assets=fix.assets, mode="fixture",
                                error=f"zabbix online failed ({e!r}); used fixture")

        assets: List[RawAsset] = []
        for h in hosts:
            iface = (h.get("interfaces") or [{}])[0]
            tags = {t.get("tag"): t.get("value") for t in h.get("tags", [])}
            assets.append(RawAsset(
                source=SOURCE_ZABBIX, source_id=str(h.get("hostid") or h.get("host") or ""),
                hostname=h.get("host", ""), fqdn=(iface.get("dns") or ""),
                ip=iface.get("ip", ""),
                attrs={"name": h.get("name"),
                       "groups": [g.get("name") for g in h.get("groups", [])],
                       "tags": tags, "utilization": {}},
            ))
        # utilization: try to pull a few key items per host. This is best-effort
        # and tolerant of missing items. Map Zabbix item keys to the normalized
        # utilization field names so normalize() picks them up (the old code
        # stored under "utilization_raw" with raw Zabbix keys, which normalize
        # never read — every online host silently lost its utilization).
        _KEY_MAP = {
            "system.cpu.util": "cpu_p95",
            "vm.memory.util": "mem_p95",
            "vfs.fs.util": "disk_used_pct",
        }
        try:
            for a in assets:
                items = call("item.get", {
                    "hostids": a.source_id, "output": ["itemid", "key_", "name"],
                    # array value + searchByAny ORs the keys; a single comma
                    # string matched nothing (Zabbix substring-matched the
                    # whole literal).
                    "search": {"key_": list(_KEY_MAP.keys())},
                    "searchByAny": True, "limit": 20,
                })
                util: Dict[str, Any] = {}
                for it in items:
                    key = it.get("key_", "")
                    # Zabbix item keys carry a [...] parameter suffix
                    # (e.g. system.cpu.util[all]); strip it before the exact
                    # map lookup, or every online host loses its utilization.
                    key_base = key.split("[", 1)[0]
                    field = _KEY_MAP.get(key_base)
                    if not field:
                        continue
                    hist = call("history.get", {"itemids": it["itemid"], "history": 0,
                                                "output": "extend", "sortfield": "clock",
                                                "sortorder": "DESC", "limit": 1})
                    if hist:
                        try:
                            util[field] = float(hist[0].get("value"))
                        except (TypeError, ValueError):
                            continue
                a.attrs["utilization"] = util
        except Exception:
            pass
        return IngestResult(assets=assets, mode="online")