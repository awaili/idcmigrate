"""Data-gap — shadow-IT / CMDB-drift discovery tests (Gap1).

Covers ``ingest.discovery.discover_drift`` (the pure diff: unknown_hosts /
cmdb_orphans / drifted, off-by-default), the Store snapshot roundtrip, and the
backend POST/GET discovery endpoints.
"""
import json
import os
import tempfile

import pytest

from idc.core.ingest.discovery import discover_drift
from idc.core.models import Server, _new_id


class _Settings:
    """Minimal stand-in for config.Settings — only discovery_path is read."""
    def __init__(self, path=""):
        self.discovery_path = path


def _write(records):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(records, f)
    return path


# -- discover_drift (pure) ------------------------------------------------
def test_discovery_off_when_path_unset():
    diff = discover_drift(_Settings(""), [Server(id="s1", hostname="h1")])
    assert diff["source"] == "off"
    assert diff["unknown_hosts"] == [] and diff["cmdb_orphans"] == []
    assert "off" in diff["summary"]


def test_discovery_unknown_hosts_are_shadow_it():
    # CMDB has h1; discovery sees h1 + a host the CMDB doesn't know (shadow IT)
    path = _write([
        {"hostname": "h1", "ips": ["10.0.0.1"], "source": "vcenter", "role": "web"},
        {"hostname": "shadow-99", "ips": ["10.0.9.9"], "source": "subnet-sweep",
         "role": "app", "os": "centos"},
    ])
    servers = [Server(id="s1", hostname="h1", fqdn="h1.dc1", ips=["10.0.0.1"],
                     role="web", os="centos")]
    diff = discover_drift(_Settings(path), servers)
    os.unlink(path)
    unknown = {h["hostname"] for h in diff["unknown_hosts"]}
    assert "shadow-99" in unknown
    assert "h1" not in unknown
    assert diff["unknown_hosts"][0]["reason"].startswith("no CMDB")


def test_discovery_cmdb_orphans_are_zombie_candidates():
    # CMDB has h1 + h2; discovery only sees h1 -> h2 is an orphan
    path = _write([{"hostname": "h1", "ips": ["10.0.0.1"], "source": "vcenter"}])
    servers = [
        Server(id="s1", hostname="h1", ips=["10.0.0.1"], role="web"),
        Server(id="s2", hostname="h2", ips=["10.0.0.2"], role="db"),
    ]
    diff = discover_drift(_Settings(path), servers)
    os.unlink(path)
    orphans = {h["hostname"] for h in diff["cmdb_orphans"]}
    assert "h2" in orphans and "h1" not in orphans


def test_discovery_drifted_when_role_or_os_differ():
    path = _write([{"hostname": "h1", "ips": ["10.0.0.1"], "source": "vcenter",
                    "role": "cache", "os": "ubuntu"}])
    servers = [Server(id="s1", hostname="h1", ips=["10.0.0.1"],
                      role="db", os="centos")]
    diff = discover_drift(_Settings(path), servers)
    os.unlink(path)
    fields = {(d["field"], d["cmdb"], d["discovered"]) for d in diff["drifted"]}
    assert ("role", "db", "cache") in fields
    assert ("os", "centos", "ubuntu") in fields
    # not counted as unknown/orphan (it matched)
    assert diff["unknown_hosts"] == []
    assert diff["cmdb_orphans"] == []


def test_discovery_matching_by_ip_only():
    # discovery record has only an IP that matches a CMDB server's ip
    path = _write([{"ips": ["10.0.0.5"], "source": "subnet-sweep", "role": "web"}])
    servers = [Server(id="s1", hostname="h1", ips=["10.0.0.5"], role="app")]
    diff = discover_drift(_Settings(path), servers)
    os.unlink(path)
    assert diff["unknown_hosts"] == []  # matched by ip -> not shadow IT
    # role drift: cmdb app vs discovered web
    assert any(d["field"] == "role" for d in diff["drifted"])


# -- DB-backed: store snapshot + backend endpoints -------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.db import open_store
from idc.config import get_settings

client = TestClient(m.app)


def _store():
    return open_store(get_settings().db_url)


def _cleanup(sids):
    st = _store()
    with st.tx() as cur:
        for sid in sids:
            cur.execute(st._x("DELETE FROM matches WHERE server_id=?"), (sid,))
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
        cur.execute(st._x("DELETE FROM discovery_snapshots WHERE id=?"), ("latest",))
    st.close()


def test_store_discovery_snapshot_roundtrip():
    st = _store()
    try:
        payload = {"unknown_hosts": [{"hostname": "x"}], "cmdb_orphans": [],
                   "drifted": [], "source": "fixture", "scanned_at": "now",
                   "summary": "1 unknown"}
        st.save_discovery(payload)
        got = st.get_discovery()
        assert got is not None
        assert got["payload"]["unknown_hosts"][0]["hostname"] == "x"
        # overwrite works (single 'latest' row)
        st.save_discovery({"unknown_hosts": [], "cmdb_orphans": [],
                           "drifted": [], "source": "off", "scanned_at": "n2",
                           "summary": "none"})
        assert st.get_discovery()["payload"]["scanned_at"] == "n2"
    finally:
        with st.tx() as cur:
            cur.execute(st._x("DELETE FROM discovery_snapshots WHERE id=?"), ("latest",))
        st.close()


def test_backend_discovery_scan_and_diff():
    sid = _new_id("srv")
    old_path = m.settings.discovery_path
    path = _write([
        {"hostname": f"shadow-{sid[-4:]}", "ips": ["10.0.9.9"], "source": "subnet-sweep",
         "role": "app", "os": "centos"},
    ])
    try:
        m.settings.discovery_path = path
        st = _store()
        st.upsert_server(Server(id=sid, hostname=f"cmdb-{sid[-4:]}",
                                 ips=["10.0.0.1"], role="web"))
        st.close()
        # GET before any scan -> 404
        assert client.get("/api/discovery/diff").status_code == 404
        # POST scan -> diff with the shadow-IT host
        r = client.post("/api/discovery/scan")
        assert r.status_code == 200, r.text
        diff = r.json()
        assert any(h["hostname"] == f"shadow-{sid[-4:]}" for h in diff["unknown_hosts"])
        # persisted -> GET diff now returns it
        r2 = client.get("/api/discovery/diff")
        assert r2.status_code == 200
        assert any(h["hostname"] == f"shadow-{sid[-4:]}" for h in r2.json()["unknown_hosts"])
    finally:
        m.settings.discovery_path = old_path
        os.unlink(path)
        _cleanup([sid])


def test_backend_discovery_scan_off_when_unset():
    old_path = m.settings.discovery_path
    try:
        m.settings.discovery_path = ""
        r = client.post("/api/discovery/scan")
        assert r.status_code == 200
        assert r.json()["source"] == "off"
        assert r.json()["unknown_hosts"] == []
    finally:
        m.settings.discovery_path = old_path