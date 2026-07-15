"""ServiceNow CMDB adapter.

Online: GET /api/now/table/{table} with basic-auth (or bearer token).
Offline: read the bundled ``servicenow_cmdb_ci_server.csv`` fixture.

We map the CMDB ``cmdb_ci_server`` (and similar) columns into RawAsset.attrs,
keeping hostname/fqdn/ip as top-level fields for entity resolution.
"""
from __future__ import annotations

import csv
from typing import Any, Dict, List

from ...config import Settings
from ..models import RawAsset, SOURCE_SERVICENOW
from .base import Adapter, IngestResult, register


def _pick(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k])
    return ""


@register
class ServiceNowAdapter(Adapter):
    source = SOURCE_SERVICENOW

    def fetch(self, settings: Settings) -> IngestResult:
        if settings.has_servicenow():
            return self._online(settings)
        if not settings.allow_fixture_fallback:
            return self._fixture_disabled(settings)
        return self._fixture(settings)

    # -- offline -----------------------------------------------------------
    def _fixture(self, settings: Settings) -> IngestResult:
        path = settings.sn_path
        assets: List[RawAsset] = []
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    assets.append(self._row_to_asset(row))
        except OSError as e:
            return IngestResult(assets=[], mode="fixture",
                                error=f"servicenow file not found: {path} ({e!r})")
        return IngestResult(assets=assets, mode="fixture")

    def _row_to_asset(self, row: Dict[str, str]) -> RawAsset:
        sys_id = _pick(row, "sys_id", "sysId", "id")
        hostname = _pick(row, "hostname", "name", "short_description")
        fqdn = _pick(row, "fqdn", "fqdn_name")
        ip = _pick(row, "ip_address", "ip_address_v4", "ip")
        return RawAsset(
            source=SOURCE_SERVICENOW, source_id=sys_id or hostname,
            hostname=hostname, fqdn=fqdn, ip=ip,
            attrs={k: v for k, v in row.items() if k},
        )

    # -- online ------------------------------------------------------------
    def _online(self, settings: Settings) -> IngestResult:
        import httpx  # local import keeps core importable without httpx

        base = settings.sn_base.rstrip("/")
        url = f"{base}/api/now/table/{settings.sn_table}"
        params = {
            "sysparm_limit": settings.sn_limit,
            "sysparm_exclude_reference_link": "true",
            "sysparm_display_value": "false",
        }
        headers = {"Accept": "application/json"}
        if settings.sn_token:
            headers["Authorization"] = f"Bearer {settings.sn_token}"
        else:
            import base64
            cred = base64.b64encode(f"{settings.sn_user}:{settings.sn_password}".encode()).decode()
            headers["Authorization"] = f"Basic {cred}"
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=60)
            r.raise_for_status()
            rows = r.json().get("result", [])
        except Exception as e:
            if not settings.allow_fixture_fallback:
                # never pull dummy fixture data over a real (creds-configured)
                # live deployment on a transient API failure — that would
                # inject phantom CMDB hosts into the real inventory.
                return IngestResult(assets=[], mode="error",
                                    error=f"servicenow online failed ({e!r}); fixture fallback disabled")
            # fall back to fixture so a demo/offline system stays usable
            fix = self._fixture(settings)
            return IngestResult(assets=fix.assets, mode="fixture",
                                error=f"servicenow online failed ({e!r}); used fixture")
        assets = [self._row_to_asset(row) for row in rows]
        return IngestResult(assets=assets, mode="online")