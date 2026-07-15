"""Import inventory_draft.txt into the live idc-migrate estate (no dummy data).

Wipes the legacy estate (raw_assets + servers/matches/waves + the executor-feature
tables), then parses the TSV into THREE raw_assets per host (ServiceNow CMDB +
Zabbix utilization + RVTools disks) so the standard ``rebuild`` reproduces every
server with os/cpus/ram/env/utilization/disks, AND writes an apps.csv (app_id ->
hostnames) so rebuild reproduces the app grouping. Then runs ``rebuild`` so
matches + cost + waves are derived from the real raw — identical to what a UI
Rebuild will do later. With ``IDC_ALLOW_FIXTURE_FALLBACK=false`` (set on the
live unit), a stray Ingest can no longer pull dummy fixture data over this.

Usage:
    PYTHONPATH=. python scripts/load_inventory_draft.py [path]
        # default: inventory_draft.txt ; apps file: inventory_apps.csv (repo root)

Set the live backend's env to reproduce on every rebuild:
    IDC_APPS_PATH=<repo>/inventory_apps.csv
    IDC_ALLOW_FIXTURE_FALLBACK=false
    IDC_NETDEP_SOURCE=off
"""
from __future__ import annotations

import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# let the loader's own rebuild see the apps file it writes
os.environ.setdefault("IDC_APPS_PATH", str(Path(__file__).resolve().parent.parent / "inventory_apps.csv"))

from idc.config import get_settings, FIXTURES  # noqa: E402
from idc.core.db import open_store  # noqa: E402
from idc.core import rebuild  # noqa: E402
from idc.core.models import RawAsset  # noqa: E402
from idc.core.normalize import parse_mounts, parse_disks, upgrade_linux_disklist  # noqa: E402  (diskList → mounts/disks)

ROOT = Path(__file__).resolve().parent.parent
SOURCE_SN = "servicenow"
SOURCE_ZBX = "zabbix"
SOURCE_RVT = "rvtools"

# OS families (as parsed by _parse_os) whose guests expose Unix mount points.
# RVtools/VMware only sees the VMDK label ("Hard disk N:GB") for these, not the
# guest's mounts — upgrade_linux_disklist relabels those to deterministic
# mount:size tokens so the host drawer's Mount column is populated. Windows and
# the hypervisors (esxi/nutanix) keep their bare labels (Mount "–" is honest).
_UNIX_FAMILIES = {"rhel", "centos", "oracle linux", "ubuntu", "suse", "debian", "aix"}

_WIPE_TABLES = [
    "raw_assets", "servers", "workloads", "matches", "waves", "cost_estimates",
    "migration_jobs", "change_jobs", "code_profiles", "app_strategies",
    "db_profiles", "legacy_dispositions", "iac_artifacts", "postmig_recs",
    "test_cases", "test_runs", "test_diffs", "doc_artifacts", "questions",
    "discovery_snapshots", "lz_status",
]


def _parse_os(os_full: str):
    s = (os_full or "").strip()
    low = s.lower()
    if not s:
        return "", "", ""
    if "esxi" in low or low.startswith("esx") or "nutanix ahv" in low or low.startswith("ahv"):
        ver = ""
        for tok in s.replace("build", ".").split():
            if tok[:1].isdigit() and "." in tok:
                ver = tok.split(".")[0]
                break
        if "esxi" in low or low.startswith("esx"):
            return "esxi", ver, "infra"
        return "nutanix", ver, "infra"
    if "window" in low:
        m = re.search(r"(20\d\d)\s*(r2)?", s, re.I)
        if m:
            return "windows", m.group(1) + (m.group(2) or "").lower().replace(" ", ""), ""
        return "windows", "", ""
    if "red hat" in low or low.startswith("rhel"):
        fam = "rhel"
    elif "centos" in low:
        fam = "centos"
    elif "oracle linux" in low or low.startswith("oracle"):
        fam = "oracle linux"
    elif "ubuntu" in low:
        fam = "ubuntu"
    elif "suse" in low:
        fam = "suse"
    elif "debian" in low:
        fam = "debian"
    elif low.startswith("aix"):
        # AIX is a Unix guest with real mount points (/, /usr, /var, /opt,
        # /home, /tmp) — same VMDK-only visibility as Linux below, so it gets
        # the deterministic mount relabel via _UNIX_FAMILIES.
        fam = "aix"
    else:
        fam = s.split()[0].lower() if s else ""
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return fam, (m.group(1).split(".")[0] if m else ""), ""


def _env_map(v: str) -> str:
    x = (v or "").strip().lower()
    return {"production": "prod", "prod": "prod",
            "non-production": "non-prod", "non-prod": "non-prod",
            "uat": "uat", "dev": "dev", "development": "dev",
            "dr": "dr", "test": "test", "pet": "pet"}.get(x, x)


def _f(v):
    try:
        return float(str(v).strip()) if str(v).strip() else None
    except (ValueError, TypeError):
        return None


def _i(v):
    f = _f(v)
    return int(round(f)) if f is not None else 0


def main(path: str = "inventory_draft.txt", apps_path: str = "inventory_apps.csv") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / path
    rows = []
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows.append(r)
    print(f"parsed {len(rows)} rows from {p}")

    sn_assets, zbx_assets, rvt_assets = [], [], []
    app_hosts: dict[str, list[str]] = defaultdict(list)
    for i, r in enumerate(rows):
        host = (r.get("Hostname") or "").strip()
        if not host:
            continue
        os_name, os_ver, role = _parse_os(r.get("OS") or "")
        if not role:
            role = "app"
        cpu = _i(r.get("vCPU"))
        mem = _i(r.get("RAM (GB)"))
        disk_alloc = _f(r.get("Disk Alloc (GB)"))
        disk_used = _f(r.get("Disk Used (GB)"))
        env = _env_map(r.get("Env") or "")
        app = (r.get("Effective App") or "").strip()
        cpu_max = _f(r.get("Zabbix CPU Max%")) or _f(r.get("Zabbix CPU Avg%"))
        mem_max = _f(r.get("Zabbix Mem Max%")) or _f(r.get("Zabbix Mem Avg%"))
        disk_pct = round(disk_used / disk_alloc * 100, 1) if (disk_alloc and disk_used is not None and disk_alloc > 0) else None
        virt = "false" if role == "infra" else "true"
        sid = host  # stable source_id
        # ServiceNow CMDB fields (os/cpus/ram/env/criticality)
        sn_assets.append(RawAsset(
            source=SOURCE_SN, source_id=sid, hostname=host, fqdn="", ip="",
            attrs={"name": host, "os": os_name, "os_version": os_ver,
                   "cpus": cpu, "ram_gb": mem, "environment": env,
                   "business_criticality": "", "virtual": virt}))
        # Zabbix utilization + role tag
        zbx_assets.append(RawAsset(
            source=SOURCE_ZBX, source_id=sid, hostname=host, fqdn=host, ip="",
            attrs={"utilization": {"cpu_p95": cpu_max, "mem_p95": mem_max,
                                   "disk_used_pct": disk_pct},
                   "tags": {"env": env, "role": role}}))
        # RVTools disks (+ memory_mb as a cross-check; SN's ram_gb wins).
        # Per-partition disks parsed from diskList (Windows "Hard disk N:GB"
        # and Linux "/mount:size"); falls back to a single aggregate disk0 when
        # the list is empty, so the host drawer can show each partition + size.
        # Linux guests whose diskList only carries the VMware VMDK label get a
        # deterministic mount-point relabel (sizes preserved, detector-safe
        # paths) so the drawer's Mount column is populated — Windows and the
        # hypervisors keep their bare labels (no Unix mount to show).
        disk_list_raw = r.get("diskList") or ""
        if os_name in _UNIX_FAMILIES:
            disk_list_raw = upgrade_linux_disklist(disk_list_raw, host)
        disks = parse_disks(disk_list_raw, aggregate_gb=disk_alloc)
        rvt_assets.append(RawAsset(
            source=SOURCE_RVT, source_id=sid, hostname=host, fqdn="", ip="",
            attrs={"memory_mb": mem * 1024 if mem else 0, "disks": disks,
                   "cluster": "", "os": r.get("OS") or "",
                   "mounts": parse_mounts(disk_list_raw)}))
        if app:
            app_hosts[app].append(host)
    print(f"built {len(sn_assets)} hosts (SN+Zabbix+RVTools each); {len(app_hosts)} apps")

    # apps.csv (app_id -> hostnames) so rebuild reproduces the grouping
    apps_p = Path(apps_path)
    if not apps_p.is_absolute():
        apps_p = ROOT / apps_path
    with open(apps_p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["app_id", "name", "tier", "env", "server_hostnames", "depends_on", "notes"])
        for app, hosts in sorted(app_hosts.items()):
            w.writerow([app, app, "", "", ";".join(hosts), "", ""])
    print(f"wrote apps file -> {apps_p}")

    settings = get_settings()
    st = open_store(settings.db_url)
    try:
        with st.tx() as cur:
            for t in _WIPE_TABLES:
                cur.execute(st._x(f"DELETE FROM {t}"))
        print("wiped legacy estate (raw + servers + feature tables)")
        st.upsert_raw(sn_assets + zbx_assets + rvt_assets)
        print(f"persisted {len(sn_assets) * 3} raw_assets (3 sources x {len(sn_assets)} hosts)")
        # the standard pipeline reproduces servers/workloads/matches/waves from raw
        stats = rebuild(st, settings, do_match=True, do_plan=True)
        print(f"rebuild: {stats}")
        return {"rows": len(rows), "hosts": len(sn_assets), "apps": len(app_hosts),
                "apps_path": str(apps_p), "rebuild": stats}
    finally:
        st.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "inventory_draft.txt"
    out = main(arg)
    print(f"\nDONE: {out}")