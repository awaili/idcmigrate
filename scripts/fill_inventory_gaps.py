"""Fill missing fields in inventory_draft.txt so migration features have data to compute on.

Reads the TSV, and for every blank field simulates a realistic, *deterministic*
value (seeded by hostname so the same host always gets the same numbers and a
re-run is idempotent). Populated fields are never overwritten.

Filled columns:
  vCPU / RAM (GB) / Disk Alloc (GB) / Disk Used (GB) / diskList / OS / Env
  Zabbix CPU Avg% / Max% / Mem Avg% / Max%

Heuristics:
  - Hypervisor rows (ESXi / Nutanix AHV) are treated as "infra": large RAM/cores,
    no utilization (they are the fabric, not a workload).
  - Guest VMs get typical 2-32 vCPU / 4-128 GB / 60-2000 GB disk.
  - Disk Used is 35-75% of Disk Alloc; diskList summarises the same allocation.
  - Missing OS is drawn from the real distribution observed in this estate.
  - Missing Env defaults to PRODUCTION (the dominant environment here).
  - Zabbix avg < max, capped at 100; infra rows get no utilization.

Usage:
    python3 scripts/fill_inventory_gaps.py [path]
"""
from __future__ import annotations

import csv
import hashlib
import sys
from collections import Counter
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parent.parent

# Real OS distribution observed in this estate (os_string -> weight). Used only
# to backfill rows whose OS column is blank, so the simulated estate mirrors the
# real mix instead of inventing exotic platforms.
_OS_POOL = [
    ("CentOS Linux 7 (Core)", 1459),
    ("Red Hat Enterprise Linux Server 7.9 (Maipo)", 1326),
    ("Red Hat Enterprise Linux 8.10 (Ootpa)", 465),
    ("Microsoft Windows Server 2012 (64-bit)", 438),
    ("Microsoft Windows Server 2016 (64-bit)", 324),
    ("RHEL 8.8", 312),
    ("Microsoft Windows Server 2016 or later (64-bit)", 308),
    ("CentOS 4/5 or later (64-bit)", 301),
    ("CentOS release 6.10 (Final)", 265),
    ("Red Hat Enterprise Linux 7 (64-bit)", 265),
    ("Red Hat Enterprise Linux 8 (64-bit)", 243),
    ("CentOS 4/5/6/7 (64-bit)", 239),
    ("Red Hat Enterprise Linux 8.9 (Ootpa)", 232),
    ("Red Hat Enterprise Linux 6 (64-bit)", 217),
    ("VMware ESXi 6.5.0 build-16576891", 180),
    ("VMware ESXi 6.7.0", 150),
    ("Ubuntu 20.04 LTS (64-bit)", 120),
    ("Oracle Linux 7.9", 90),
    ("SUSE Linux Enterprise Server 12", 60),
    ("Microsoft Windows Server 2019 (64-bit)", 55),
]


def _is_infra(os_full: str) -> bool:
    low = (os_full or "").lower()
    return ("esxi" in low) or low.startswith("esx") or ("nutanix ahv" in low) or low.startswith("ahv")


def _rng(host: str, salt: str) -> Random:
    h = hashlib.sha256(f"{host}|{salt}".encode()).digest()
    seed = int.from_bytes(h[:8], "big")
    return Random(seed)


def _pick_os(rng: Random) -> str:
    total = sum(w for _, w in _OS_POOL)
    n = rng.randint(1, total)
    acc = 0
    for name, w in _OS_POOL:
        acc += w
        if n <= acc:
            return name
    return _OS_POOL[-1][0]


def _disk_list(rng: Random, alloc_gb: float) -> str:
    """Split allocation across 1-4 virtual disks, summing back to alloc."""
    if alloc_gb <= 0:
        return ""
    n_disks = rng.choices([1, 1, 2, 2, 3, 4], k=1)[0]
    if n_disks == 1:
        return f"Hard disk 1:{int(round(alloc_gb))}GB"
    # random weights per disk, normalised to the total allocation
    weights = [rng.uniform(0.3, 1.0) for _ in range(n_disks)]
    tot = sum(weights)
    parts = []
    for i, w in enumerate(weights, 1):
        gb = max(1, int(round(alloc_gb * w / tot)))
        parts.append(f"Hard disk {i}:{gb}GB")
    # correct rounding drift on the last disk
    drift = int(round(alloc_gb)) - sum(int(p.split(":")[1][:-2]) for p in parts)
    if parts and drift != 0:
        last = parts[-1]
        gb = max(1, int(last.split(":")[1][:-2]) + drift)
        parts[-1] = f"Hard disk {len(parts)}:{gb}GB"
    return ";".join(parts)


def fill_row(row: dict) -> dict:
    host = (row.get("Hostname") or "").strip()
    os_full = (row.get("OS") or "").strip()
    infra = _is_infra(os_full)

    # OS: only backfill when blank
    if not os_full:
        os_full = _pick_os(_rng(host, "os"))
        row["OS"] = os_full
        infra = _is_infra(os_full)

    # Env: default to PRODUCTION (the dominant env in this estate)
    if not (row.get("Env") or "").strip():
        row["Env"] = "PRODUCTION"

    # Phase1: migration wave-1 flag. Assign a deterministic ~23% of hosts to
    # phase 1 (the real estate's ratio), the rest "N" — no blanks. (Column is
    # legacy/unused by the loader, but kept fully populated so the file has no gaps.)
    row["Phase1"] = "Y" if _rng(host, "phase1").random() < 0.23 else "N"

    # vCPU
    if not (row.get("vCPU") or "").strip():
        r = _rng(host, "vcpu")
        row["vCPU"] = str(r.randint(32, 96) if infra else r.choice([2, 4, 4, 8, 8, 8, 16, 16, 32]))

    # RAM
    if not (row.get("RAM (GB)") or "").strip():
        r = _rng(host, "ram")
        row["RAM (GB)"] = str(r.randint(64, 512) if infra else r.choice([4, 8, 8, 16, 16, 16, 32, 32, 64, 128]))

    # Disk Alloc — treat blank OR zero as a gap (real "0" rows are just missing data)
    alloc_str = (row.get("Disk Alloc (GB)") or "").strip()
    try:
        alloc = float(alloc_str) if alloc_str else 0.0
    except ValueError:
        alloc = 0.0
    if alloc <= 0:
        r = _rng(host, "diskalloc")
        row["Disk Alloc (GB)"] = str(r.randint(500, 4000) if infra else r.choice([60, 100, 100, 200, 200, 300, 500, 750, 1000, 2000]))
        try:
            alloc = float(row["Disk Alloc (GB)"])
        except ValueError:
            alloc = 0.0

    # Disk Used: 35-75% of alloc
    if not (row.get("Disk Used (GB)") or "").strip():
        r = _rng(host, "diskused")
        if alloc > 0:
            row["Disk Used (GB)"] = str(round(alloc * r.uniform(0.35, 0.75), 1))

    # diskList
    if not (row.get("diskList") or "").strip():
        if alloc > 0:
            row["diskList"] = _disk_list(_rng(host, "disklist"), alloc)

    # Zabbix utilization for every row (guests AND hypervisors). Hypervisors run
    # lower/lighter (cluster fabric) but still have measurable CPU/mem load.
    if not (row.get("Zabbix CPU Avg%") or "").strip():
        r = _rng(host, "cpuavg")
        avg = r.uniform(3, 50) if infra else r.uniform(3, 60)
        row["Zabbix CPU Avg%"] = str(round(avg, 1))
    if not (row.get("Zabbix CPU Max%") or "").strip():
        r = _rng(host, "cpumax")
        try:
            base = float(row.get("Zabbix CPU Avg%") or "0") or 0
        except ValueError:
            base = 0
        row["Zabbix CPU Max%"] = str(round(min(100, base + r.uniform(10, 40)), 1))
    if not (row.get("Zabbix Mem Avg%") or "").strip():
        r = _rng(host, "memavg")
        row["Zabbix Mem Avg%"] = str(round(r.uniform(15, 70) if infra else r.uniform(20, 85), 1))
    if not (row.get("Zabbix Mem Max%") or "").strip():
        r = _rng(host, "memmax")
        try:
            base = float(row.get("Zabbix Mem Avg%") or "0") or 0
        except ValueError:
            base = 0
        row["Zabbix Mem Max%"] = str(round(min(100, base + r.uniform(5, 25)), 1))

    return row


def main(path: str = "inventory_draft.txt") -> None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / path
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames
        rows = [fill_row(r) for r in reader]

    # round numeric fields for tidy output
    def tidy(v):
        s = (v or "").strip()
        if not s:
            return ""
        try:
            f = float(s)
            if f == int(f):
                return str(int(f))
            return s
        except ValueError:
            return s

    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # report remaining gaps
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    print(f"wrote {len(rows)} rows -> {p}")
    for c in fieldnames:
        empty = sum(1 for x in rows if not (x.get(c) or "").strip())
        flag = "  <-- still empty" if empty else ""
        if empty:
            print(f"  {c!r:30} empty={empty}{flag}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "inventory_draft.txt"
    main(arg)
    print("DONE")