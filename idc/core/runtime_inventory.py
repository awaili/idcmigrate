"""F9 — runtime inventory gatherer for the no-source containerization path.

Assembles a best-effort ``{process, ports, software}`` inventory for a server so
the operator doesn't have to hand-fill the F9 runtime-containerize form. Pulls
from signals idc-migrate already has:

  * ``Match.target.extras.port`` — the service port the match already derived.
  * ``Server.os`` / ``Server.role`` — a software basis (role=db → mysql/oracle,
    role=cache → redis, role=web → nginx/apache; OS hints the base image).
  * an optional ``CodeProfile`` (language/runtime/framework) when one exists
    (the F9 path is for source-less apps, but a stale/legacy profile may exist).
  * Zabbix listening-port items (``net.tcp.listen`` / ``net.tcp.port``) when
    configured — the strongest "what's actually running" signal.
  * Prometheus ``node_netstat_tcp_info`` listening ports when configured.

The remote pulls are best-effort and tolerant of failure (no items / wrong keys
/ unreachable → empty list, never raise). ``basis`` records which fields were
measured vs inferred so the executor + operator know how much to trust the
scaffold. Pure w.r.t. the server/match inputs; the telemetry pulls are lazy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import Match, Server

# role → likely software (the runtime-containerize scaffold hints)
_ROLE_SOFTWARE = {
    "db": ["mysql", "oracle"], "cache": ["redis"], "web": ["nginx", "apache2"],
    "app": [], "monitoring": ["prometheus", "grafana"], "k8s": ["kubelet", "containerd"],
    "hadoop": ["hadoop", "jdk"],
}
# OS → Dockerfile base image hint
_OS_BASE = {
    "centos": "centos:7", "oracle linux": "oraclelinux:8-slim",
    "ubuntu": "ubuntu:22.04", "debian": "debian:12-slim", "rhel": "ubi9/ubi:9",
    "windows": "windowsservercore:ltsc2022",
}
# common port → service guesses (a measured port is a stronger signal than role)
_PORT_SERVICE = {
    80: "nginx", 443: "nginx", 8080: "app-server", 3306: "mysql", 1521: "oracle",
    5432: "postgres", 6379: "redis", 9200: "elasticsearch", 9090: "prometheus",
    3000: "grafana", 22: "sshd", 2181: "zookeeper", 9092: "kafka",
}


def _from_match(match: Optional[Match]) -> List[int]:
    if not match or not match.target:
        return []
    p = match.target.extras.get("port")
    try:
        return [int(p)] if p else []
    except (TypeError, ValueError):
        return []


def _from_profile(profile) -> Dict[str, Any]:
    """Software hints from a CodeProfile (legacy/stale profile may exist even
    for a source-less app — bonus signal, not required)."""
    if not profile:
        return {}
    sw: List[str] = []
    if profile.runtime:
        sw.append(profile.runtime)
    if profile.language and profile.language not in sw:
        sw.append(profile.language)
    if profile.framework and profile.framework not in sw:
        sw.append(profile.framework)
    return {"software": sw, "process_hint": (profile.runtime or "")}


def _zabbix_ports(settings, host: str) -> List[int]:
    """Best-effort: pull listening-port item values from Zabbix for one host.
    Tolerant of any failure (wrong item keys / unreachable / no items)."""
    if not settings.has_zabbix() or not host:
        return []
    import httpx
    url = settings.zbx_url
    rid = 0
    auth_token = settings.zbx_token

    def call(method, params):
        nonlocal rid
        rid += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
        if auth_token:
            payload["auth"] = auth_token
        r = httpx.post(url, json=payload, timeout=20)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(j["error"])
        return j.get("result")

    try:
        if not auth_token and settings.zbx_user and settings.zbx_password:
            auth_token = call("user.login", {"user": settings.zbx_user,
                                             "password": settings.zbx_password})
        # find the hostid by host name
        hosts = call("host.get", {"output": ["hostid"], "filter": {"host": [host]}})
        if not hosts:
            return []
        hostid = hosts[0]["hostid"]
        items = call("item.get", {
            "hostids": hostid, "output": ["itemid", "key_"],
            "search": {"key_": ["net.tcp.listen", "net.tcp.port"]},
            "searchByAny": True, "limit": 50,
        })
        ports: List[int] = []
        for it in items:
            hist = call("history.get", {"itemids": it["itemid"], "history": 0,
                                        "output": "extend", "sortfield": "clock",
                                        "sortorder": "DESC", "limit": 1})
            if hist:
                try:
                    v = int(hist[0].get("value", 0))
                    if v:
                        ports.append(v)
                except (TypeError, ValueError):
                    continue
        return sorted(set(ports))
    except Exception:
        return []


def _prometheus_ports(settings, host: str) -> List[int]:
    """Best-effort: listening TCP ports from Prometheus node_netstat. The
    ``node_netstat_tcp_info`` / ``ss`` based query needs node_exporter; tolerant
    of any failure. Returns the local-listening port numbers."""
    if not settings.has_prometheus() or not host:
        return []
    import httpx
    # node_exporter's `node_netstat_tcp_info` doesn't directly list ports; the
    # practical signal is `node_tcp_connections` labelled by state, which doesn't
    # give ports either. The real port list comes from `ss -tlnp` textfile exporter.
    # We query a conservative textfile metric name that's common in practice; if
    # absent, we return [] (the caller falls back to match port).
    try:
        _h = (host or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        r = httpx.get(f"{settings.prom_url}/api/v1/query",
                      params={"query": f'node_port_listen{{instance="{_h}"}}'},
                      timeout=settings.prom_timeout)
        if r.status_code != 200:
            return []
        res = (r.json().get("data") or {}).get("result") or []
        ports: List[int] = []
        for s in res:
            try:
                ports.append(int(s["metric"].get("port") or s["value"][1]))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(set(ports))
    except Exception:
        return []


def gather_runtime_inventory(server: Server, match: Optional[Match] = None,
                              profile=None, settings=None) -> Dict[str, Any]:
    """Assemble a best-effort runtime inventory (F9) for a server.

    Returns ``{process, ports, software, base_image, basis}`` where ``basis``
    records per-field provenance (``measured`` from telemetry/match port vs
    ``inferred`` from role/os). Pure w.r.t. server/match/profile; the Zabbix/
    Prometheus pulls are best-effort (lazy, never raise).
    """
    basis: Dict[str, str] = {}
    # --- ports: match port first, telemetry augments ---------------------
    ports = _from_match(match)
    sources: List[str] = []
    if ports:
        sources.append("match")
    if settings is not None:
        zbx = _zabbix_ports(settings, server.hostname)
        if zbx:
            ports = sorted(set(ports + zbx))
            sources.append("zabbix")
        prom = _prometheus_ports(settings, server.hostname)
        if prom:
            ports = sorted(set(ports + prom))
            sources.append("prom")
    basis["ports"] = "+".join(sources) if sources else "none"

    # --- software: role + OS + (stale) profile --------------------------
    sw: List[str] = []
    role = (server.role or "").lower()
    sw.extend(_ROLE_SOFTWARE.get(role, []))
    os_name = (server.os or "").lower()
    base = _OS_BASE.get(os_name)
    prof_hint = _from_profile(profile)
    for s in prof_hint.get("software", []):
        if s and s not in sw:
            sw.append(s)
    # port → service guesses strengthen the software list (measured ports win)
    for p in ports:
        svc = _PORT_SERVICE.get(p)
        if svc and svc not in sw:
            sw.append(svc)
    basis["software"] = "inferred+profile" if prof_hint else "inferred"

    # --- process: best-effort hint (no generic source; profile runtime if any) -
    process = prof_hint.get("process_hint") or ""
    basis["process"] = "profile" if process else "none"

    return {
        "process": process,
        "ports": ports,
        "software": sw,
        "base_image": base or "",
        "role": role,
        "os": server.os or "",
        "basis": basis,
    }


def merge_inventory(gathered: Dict[str, Any], provided: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Merge a gathered inventory with operator-provided fields. Provided
    values win (operator override); gathered fills the gaps. Returns a flat
    {process, ports, software, base_image} for the executor body."""
    provided = provided or {}
    g = gathered or {}
    proc = (provided.get("process") or g.get("process") or "").strip()
    ports: List[int] = []
    for p in (provided.get("ports") or []):
        try:
            ports.append(int(p))
        except (TypeError, ValueError):
            pass
    if not ports:
        ports = list(g.get("ports") or [])
    sw = provided.get("software") or g.get("software") or []
    if isinstance(sw, str):
        sw = [s.strip() for s in sw.split(",") if s.strip()]
    return {
        "process": proc,
        "ports": ports,
        "software": list(sw),
        "base_image": (provided.get("base_image") or g.get("base_image") or ""),
    }


__all__ = ["gather_runtime_inventory", "merge_inventory"]