#!/usr/bin/env python3
"""One-off: populate the Mount column for AIX guests in the live estate.

AIX is a Unix guest with real mount points (/, /usr, /var, /opt, /home, /tmp),
but RVtools/VMware only surfaces the VMDK label ("Hard disk N:GB") for it —
same blind spot as Linux. ``scripts/load_inventory_draft.py`` now relabels AIX
via ``upgrade_linux_disklist`` at load time, but the live DB was loaded before
that fix. This script applies the identical deterministic relabel IN PLACE to
the 47 AIX hosts already in the live DB — no wipe, no full rebuild — updating
both the RVTools ``raw_assets`` attrs and the derived ``servers.disks`` so the
host drawer's Mount column populates and a future Rebuild reproduces it.

Idempotent: hosts whose disks already carry a ``/``-mount (``fs`` set) are
skipped, so re-running is a no-op. Run with the live ``IDC_DB_URL`` exported.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from idc.core.db import open_store          # noqa: E402
from idc.config import get_settings          # noqa: E402
from idc.core.normalize import (             # noqa: E402
    upgrade_linux_disklist, parse_disks, parse_mounts,
)


def _relabel(disks: list, hostname: str):
    """Rebuild the VMDK-only diskList from ``disks`` and relabel -> new disks.

    Returns ``(new_disks, mounts)`` or ``(None, None)`` if already mounted.
    """
    if not disks:
        return None, None
    if any(d.get("fs") or (d.get("name") or "").startswith("/") for d in disks):
        return None, None  # already has mounts — leave untouched (idempotent)
    disk_list = ";".join(f"{d['name']}:{d['size_gb']}GB" for d in disks if d.get("size_gb"))
    if not disk_list:
        return None, None
    upgraded = upgrade_linux_disklist(disk_list, hostname)
    return parse_disks(upgraded), parse_mounts(upgraded)


def main() -> dict:
    st = open_store(get_settings().db_url)
    srv_updated = raw_updated = skipped = 0
    try:
        with st.tx() as cur:
            cur.execute("SELECT id, hostname, disks FROM servers WHERE os LIKE 'aix%'")
            rows = cur.fetchall()

        aix_hosts = {r["hostname"]: r["id"] for r in rows}
        # 1) servers.disks
        for r in rows:
            disks = json.loads(r["disks"]) if isinstance(r["disks"], str) else r["disks"]
            new_disks, _ = _relabel(disks or [], r["hostname"])
            if new_disks is None:
                skipped += 1
                continue
            with st.tx() as cur:
                cur.execute(st._x("UPDATE servers SET disks=? WHERE id=?"),
                            (json.dumps(new_disks), r["id"]))
            srv_updated += 1

        # 2) raw_assets (rvtools) attrs.disks + attrs.mounts — so Rebuild reproduces
        if aix_hosts:
            placeholders = ",".join(["?"] * len(aix_hosts))
            with st.tx() as cur:
                cur.execute(st._x(
                    f"SELECT source_id, attrs FROM raw_assets "
                    f"WHERE source='rvtools' AND source_id IN ({placeholders})"),
                    tuple(aix_hosts))
                raws = cur.fetchall()
            for r in raws:
                attrs = json.loads(r["attrs"]) if isinstance(r["attrs"], str) else r["attrs"]
                new_disks, new_mounts = _relabel(attrs.get("disks") or [], r["source_id"])
                if new_disks is None:
                    continue
                attrs["disks"] = new_disks
                attrs["mounts"] = new_mounts
                with st.tx() as cur:
                    cur.execute(st._x(
                        "UPDATE raw_assets SET attrs=? "
                        "WHERE source='rvtools' AND source_id=?"),
                        (json.dumps(attrs), r["source_id"]))
                raw_updated += 1
        return {"aix_hosts": len(aix_hosts), "servers_updated": srv_updated,
                "raw_updated": raw_updated, "skipped_already_mounted": skipped}
    finally:
        st.close()


if __name__ == "__main__":
    print("DONE:", main())