"""Zabbix adapter.

Online: JSON-RPC ``host.get`` with tags + interfaces, then ``item.get`` /
``history.get`` for utilization (cpu/mem/disk/net). Offline: fixture JSON
that already carries per-host utilization so the pipeline is exercisable
without a live Zabbix.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ...config import FIXTURES, Settings
from ..models import RawAsset, SOURCE_ZABBIX
from .base import Adapter, IngestResult, register


@register
class ZabbixAdapter(Adapter):
    source = SOURCE_ZABBIX

    def fetch(self, settings: Settings) -> IngestResult:
        if settings.has_zabbix():
            return self._online(settings)
        return self._fixture()

    def _fixture(self) -> IngestResult:
        path = FIXTURES / "zabbix_hosts.json"
        data = json.loads(path.read_text(encoding="utf-8"))
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

        def call(method: str, params: Dict[str, Any]) -> Any:
            nonlocal rid
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
            rid += 1
            if settings.zbx_token:
                payload["auth"] = settings.zbx_token
            r = httpx.post(url, json=payload, timeout=60)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j.get("result")

        try:
            # token login only needed for user-based auth; token already set as auth
            if not settings.zbx_token:
                token = call("user.login", {
                    "username": settings.zbx_user, "password": settings.zbx_password})
                # re-issue calls with auth token by wrapping: simplest is to set env
                settings.zbx_token = token  # type: ignore[assignment]
            hosts = call("host.get", {
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip", "dns", "type"],
                "selectGroups": ["name"],
                "selectTags": ["tag", "value"],
            })
        except Exception as e:
            fix = self._fixture()
            return IngestResult(assets=fix.assets, mode="fixture",
                                error=f"zabbix online failed ({e!r}); used fixture")

        assets: List[RawAsset] = []
        for h in hosts:
            iface = (h.get("interfaces") or [{}])[0]
            tags = {t.get("tag"): t.get("value") for t in h.get("tags", [])}
            assets.append(RawAsset(
                source=SOURCE_ZABBIX, source_id=str(h.get("hostid")),
                hostname=h.get("host", ""), fqdn=(iface.get("dns") or ""),
                ip=iface.get("ip", ""),
                attrs={"name": h.get("name"),
                       "groups": [g.get("name") for g in h.get("groups", [])],
                       "tags": tags, "utilization": {}},
            ))
        # utilization: try to pull a few key items per host. This is best-effort
        # and tolerant of missing items.
        try:
            for a in assets:
                items = call("item.get", {
                    "hostids": a.source_id, "output": ["itemid", "key_", "name"],
                    "search": {"key_": "system.cpu.util,vm.memory.util,vfs.fs.util"},
                    "searchByAny": True, "limit": 20,
                })
                util: Dict[str, Any] = {}
                for it in items:
                    key = it.get("key_", "")
                    hist = call("history.get", {"itemids": it["itemid"], "history": 0,
                                                "output": "extend", "sortfield": "clock",
                                                "sortorder": "DESC", "limit": 1})
                    if hist:
                        util[key] = hist[0].get("value")
                a.attrs["utilization_raw"] = util
        except Exception:
            pass
        return IngestResult(assets=assets, mode="online")