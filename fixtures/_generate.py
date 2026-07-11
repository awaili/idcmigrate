"""Generate consistent sample fixtures for all 4 inventory sources.

Run:  python fixtures/_generate.py                 # 15 curated servers (demo + tests)
      python fixtures/_generate.py --count 15000   # synthetic 15K estate (scale test)

Produces servers spanning vm/baremetal/k8s, with shared hostnames so normalize
can demonstrate cross-source entity resolution. Real adapters hit live APIs;
these fixtures let the system run out-of-the-box and let us prove the UI
scales to a real 15K-server estate.
"""
import argparse
import csv
import hashlib
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))

# (hostname, fqdn, ip, source_type, role, os, os_ver, cpu, mem_gb, disks[(name,gb,kind,fs)], subnet, vlan, dc, cluster, env, criticality, apps, tags)
SERVERS = [
    ("app-orders-01", "app-orders-01.dc1.corp", "10.10.1.11", "vm", "app", "centos", "7.9", 4, 16,
     [("sda", 50, "ssd", "/"), ("sdb", 100, "ssd", "/data")], "10.10.1.0/24", "vlan10", "dc1", "cl-app", "prod", "high", ["app-orders"], ["java", "spring"]),
    ("app-orders-02", "app-orders-02.dc1.corp", "10.10.1.12", "vm", "app", "centos", "7.9", 4, 16,
     [("sda", 50, "ssd", "/"), ("sdb", 100, "ssd", "/data")], "10.10.1.0/24", "vlan10", "dc1", "cl-app", "prod", "high", ["app-orders"], ["java", "spring"]),
    ("app-users-01", "app-users-01.dc1.corp", "10.10.1.21", "vm", "app", "centos", "7.9", 4, 8,
     [("sda", 50, "ssd", "/"), ("sdb", 50, "ssd", "/data")], "10.10.1.0/24", "vlan10", "dc1", "cl-app", "prod", "high", ["app-users"], ["java"]),
    ("web-portal-01", "web-portal-01.dc1.corp", "10.10.2.11", "vm", "web", "centos", "8.4", 2, 4,
     [("sda", 40, "ssd", "/")], "10.10.2.0/24", "vlan20", "dc1", "cl-web", "prod", "medium", ["app-portal"], ["nginx"]),
    ("db-mysql-01", "db-mysql-01.dc1.corp", "10.10.3.11", "vm", "db", "centos", "7.9", 8, 32,
     [("sda", 50, "ssd", "/"), ("sdb", 500, "ssd", "/var/lib/mysql")], "10.10.3.0/24", "vlan30", "dc1", "cl-db", "prod", "high", ["app-orders", "app-users"], ["mysql", "primary"]),
    ("db-mysql-02", "db-mysql-02.dc1.corp", "10.10.3.12", "vm", "db", "centos", "7.9", 8, 32,
     [("sda", 50, "ssd", "/"), ("sdb", 500, "ssd", "/var/lib/mysql")], "10.10.3.0/24", "vlan30", "dc1", "cl-db", "prod", "high", ["app-orders", "app-users"], ["mysql", "replica"]),
    ("db-oracle-01", "db-oracle-01.dc1.corp", "10.10.3.21", "vm", "db", "oracle linux", "8.6", 16, 64,
     [("sda", 50, "ssd", "/"), ("sdb", 1000, "ssd", "/u01"), ("sdc", 2000, "san", "/u02")], "10.10.3.0/24", "vlan30", "dc1", "cl-db", "prod", "high", ["app-billing"], ["oracle", "19c"]),
    ("cache-redis-01", "cache-redis-01.dc1.corp", "10.10.4.11", "vm", "cache", "centos", "7.9", 4, 16,
     [("sda", 40, "ssd", "/")], "10.10.4.0/24", "vlan40", "dc1", "cl-cache", "prod", "medium", ["app-orders"], ["redis"]),
    ("k8s-master-01", "k8s-master-01.dc1.corp", "10.10.5.10", "k8s-master", "k8s", "ubuntu", "22.04", 8, 16,
     [("sda", 100, "ssd", "/")], "10.10.5.0/24", "vlan50", "dc1", "cl-k8s", "prod", "high", ["app-k8s-platform"], ["k8s", "control-plane"]),
    ("k8s-worker-01", "k8s-worker-01.dc1.corp", "10.10.5.21", "k8s-node", "k8s", "ubuntu", "22.04", 16, 64,
     [("sda", 100, "ssd", "/"), ("sdb", 500, "ssd", "/var/lib/containerd")], "10.10.5.0/24", "vlan50", "dc1", "cl-k8s", "prod", "high", ["app-k8s-platform"], ["k8s", "worker"]),
    ("k8s-worker-02", "k8s-worker-02.dc1.corp", "10.10.5.22", "k8s-node", "k8s", "ubuntu", "22.04", 16, 64,
     [("sda", 100, "ssd", "/"), ("sdb", 500, "ssd", "/var/lib/containerd")], "10.10.5.0/24", "vlan50", "dc1", "cl-k8s", "prod", "high", ["app-k8s-platform"], ["k8s", "worker"]),
    ("hdop-nn-01", "hdop-nn-01.dc2.corp", "10.20.1.10", "baremetal", "hadoop", "centos", "7.9", 16, 64,
     [("sda", 100, "ssd", "/")], "10.20.1.0/24", "vlan60", "dc2", "cl-hdop", "prod", "high", ["app-hadoop"], ["hadoop", "namenode"]),
    ("hdop-dn-01", "hdop-dn-01.dc2.corp", "10.20.1.21", "baremetal", "hadoop", "centos", "7.9", 16, 64,
     [("sda", 100, "ssd", "/"), ("sdb", 4000, "hdd", "/data1"), ("sdc", 4000, "hdd", "/data2")], "10.20.1.0/24", "vlan60", "dc2", "cl-hdop", "prod", "medium", ["app-hadoop"], ["hadoop", "datanode"]),
    ("hdop-dn-02", "hdop-dn-02.dc2.corp", "10.20.1.22", "baremetal", "hadoop", "centos", "7.9", 16, 64,
     [("sda", 100, "ssd", "/"), ("sdb", 4000, "hdd", "/data1"), ("sdc", 4000, "hdd", "/data2")], "10.20.1.0/24", "vlan60", "dc2", "cl-hdop", "prod", "medium", ["app-hadoop"], ["hadoop", "datanode"]),
    ("mon-grafana-01", "mon-grafana-01.dc1.corp", "10.10.9.11", "vm", "monitoring", "ubuntu", "22.04", 2, 4,
     [("sda", 40, "ssd", "/")], "10.10.9.0/24", "vlan90", "dc1", "cl-mon", "staging", "low", ["app-observability"], ["grafana"]),
]

APPS = [
    ("app-orders", "Orders Service", "backend", "prod", ["app-orders-01", "app-orders-02"], ["app-users"], "order processing; depends on users svc + mysql"),
    ("app-users", "Users Service", "backend", "prod", ["app-users-01"], [], "user/auth svc; depends on mysql"),
    ("app-portal", "Customer Portal", "frontend", "prod", ["web-portal-01"], ["app-users", "app-orders"], "public web; depends on users+orders"),
    ("app-billing", "Billing", "backend", "prod", ["db-oracle-01"], [], "oracle-backed billing"),
    ("app-k8s-platform", "K8s Platform", "infra", "prod", ["k8s-master-01", "k8s-worker-01", "k8s-worker-02"], [], "containerized workloads"),
    ("app-hadoop", "Hadoop Cluster", "data", "prod", ["hdop-nn-01", "hdop-dn-01", "hdop-dn-02"], [], "big data platform"),
    ("app-observability", "Observability", "infra", "staging", ["mon-grafana-01"], [], "grafana dashboards"),
]


def _util(hostname, role):
    # deterministic-ish utilization per host
    import hashlib
    h = int(hashlib.md5(hostname.encode()).hexdigest()[:6], 16)
    cpu = 20 + (h % 60)            # 20-80
    mem = 30 + ((h >> 3) % 55)     # 30-85
    disk = 25 + ((h >> 6) % 60)    # 25-85
    if role == "db":
        cpu = min(cpu + 15, 95); mem = min(mem + 20, 95)
    if role == "hadoop":
        disk = min(disk + 10, 95)
    net_rx = 10 + (h % 200)        # mbps
    net_tx = 5 + (h % 150)
    return round(cpu, 1), round(mem, 1), round(disk, 1), net_rx, net_tx


def write_servicenow():
    path = os.path.join(HERE, "servicenow_cmdb_ci_server.csv")
    cols = ["sys_id", "name", "hostname", "fqdn", "ip_address", "os", "os_version",
            "ram_gb", "cpus", "company", "environment", "business_criticality",
            "location", "virtual", "subnet", "serial_number"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, s in enumerate(SERVERS):
            hn, fqdn, ip, st, role, osn, osv, cpu, mem, disks, subnet, vlan, dc, cluster, env, crit, apps, tags = s
            w.writerow({
                "sys_id": f"SYS{i:04d}", "name": hn, "hostname": hn, "fqdn": fqdn,
                "ip_address": ip, "os": osn, "os_version": osv, "ram_gb": mem,
                "cpus": cpu, "company": "Corp", "environment": env,
                "business_criticality": crit, "location": dc,
                "virtual": "true" if st == "vm" else "false",
                "subnet": subnet, "serial_number": f"SN{700000+i}",
            })
    print("wrote", path)


def write_rvtools():
    # vInfo tab — VMs only (baremetal/k8s masters are not vSphere VMs in this mock)
    vm_hosts = [s for s in SERVERS if s[3] == "vm"]
    vinfo = os.path.join(HERE, "rvtools_vInfo.csv")
    cols = ["VM", "PowerState", "OS according to the configuration file", "CPUs",
            "Memory", "Provisioned (GB)", "Used (GB)", "Datacenter", "Cluster",
            "Host", "Network #1", "IP Address #1", "Folder"]
    with open(vinfo, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in vm_hosts:
            hn, fqdn, ip, st, role, osn, osv, cpu, mem, disks, subnet, vlan, dc, cluster, env, crit, apps, tags = s
            w.writerow({
                "VM": hn, "PowerState": "poweredOn",
                "OS according to the configuration file": f"{osn} {osv}" if osn != "windows" else osv,
                "CPUs": cpu, "Memory": mem * 1024, "Provisioned (GB)": sum(d[1] for d in disks),
                "Used (GB)": round(sum(d[1] for d in disks) * 0.5, 1), "Datacenter": dc,
                "Cluster": cluster, "Host": f"{cluster}-esx01",
                "Network #1": f"dvPG-{vlan}", "IP Address #1": ip, "Folder": f"/{env}/{role}",
            })
    print("wrote", vinfo)

    # vDisk tab
    vdisk = os.path.join(HERE, "rvtools_vDisk.csv")
    cols = ["VM", "Disk", "Capacity (GB)", "Disk Mode", "Disk Type", "Path"]
    with open(vdisk, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in vm_hosts:
            hn = s[0]; disks = s[9]
            for idx, d in enumerate(disks):
                w.writerow({"VM": hn, "Disk": f"Hard disk {idx+1}", "Capacity (GB)": d[1],
                            "Disk Mode": "persistent", "Disk Type": d[2].upper() if d[2] != "ssd" else "THIN",
                            "Path": d[3]})
    print("wrote", vdisk)

    # vNetwork tab
    vnet = os.path.join(HERE, "rvtools_vNetwork.csv")
    cols = ["VM", "Network", "IP Address", "Connected", "MAC Address"]
    with open(vnet, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in vm_hosts:
            hn = s[0]; vlan = s[11]; ip = s[2]
            w.writerow({"VM": hn, "Network": f"dvPG-{vlan}", "IP Address": ip,
                        "Connected": "TRUE", "MAC Address": "00:50:56:" + ":".join(f"{(hash(hn)%256):02x}" for _ in range(3))})
    print("wrote", vnet)


def write_zabbix():
    path = os.path.join(HERE, "zabbix_hosts.json")
    hosts = []
    for i, s in enumerate(SERVERS):
        hn, fqdn, ip, st, role, *_ = s
        cpu, mem, disk, rx, tx = _util(hn, role)
        hosts.append({
            "hostid": f"10{i:03d}", "host": hn, "name": hn,
            "interfaces": [{"ip": ip, "dns": fqdn, "type": "agent"}],
            "groups": [{"name": f"{s[14]}/{s[4]}"}],
            "tags": [{"tag": "env", "value": s[14]}, {"tag": "role", "value": role}],
            "utilization": {"cpu_p95": cpu, "mem_p95": mem, "disk_used_pct": disk,
                            "net_rx_mbps": rx, "net_tx_mbps": tx},
        })
    with open(path, "w") as f:
        json.dump({"hosts": hosts}, f, indent=2)
    print("wrote", path)


def write_prometheus():
    path = os.path.join(HERE, "prometheus_metrics.json")
    # instant-vector style results keyed by metric name; each has labels {instance=hostname}
    results = {"cpu_usage_pct": [], "mem_usage_pct": [], "disk_used_pct": [],
               "net_rx_mbps": [], "net_tx_mbps": []}
    for s in SERVERS:
        hn, fqdn, ip, st, role, *_ = s
        cpu, mem, disk, rx, tx = _util(hn, role)
        results["cpu_usage_pct"].append({"metric": {"instance": hn}, "value": [1719000000, str(cpu)]})
        results["mem_usage_pct"].append({"metric": {"instance": hn}, "value": [1719000000, str(mem)]})
        results["disk_used_pct"].append({"metric": {"instance": hn}, "value": [1719000000, str(disk)]})
        results["net_rx_mbps"].append({"metric": {"instance": hn}, "value": [1719000000, str(rx)]})
        results["net_tx_mbps"].append({"metric": {"instance": hn}, "value": [1719000000, str(tx)]})
    with open(path, "w") as f:
        json.dump({"status": "success", "data": {"result": results}}, f, indent=2)
    print("wrote", path)


def write_apps():
    path = os.path.join(HERE, "apps.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["app_id", "name", "tier", "env", "server_hostnames", "depends_on", "notes"])
        for a in APPS:
            w.writerow([a[0], a[1], a[2], a[3], ";".join(a[4]), ";".join(a[5]), a[6]])
    print("wrote", path)


# ---------------------------------------------------------------------------
# synthetic estate (for scale testing at real 15K-server volume)
# ---------------------------------------------------------------------------
def build_synthetic(n, seed=42):
    """Overwrite module-level SERVERS and APPS with n synthetic servers."""
    global SERVERS, APPS
    rnd = random.Random(seed)
    # role -> (weight, prefix, source_type, os_choices, cpu_range, mem_range, disk_spec, tags, app_tier)
    SPEC = [
        ("app", 38, "app-", "vm", [("centos", "7.9"), ("ubuntu", "22.04"), ("rhel", "8.6")],
         (2, 8), (4, 16), [(50, "ssd", "/"), (80, "ssd", "/data")], ["java"], "backend"),
        ("web", 9, "web-", "vm", [("centos", "8.4"), ("ubuntu", "22.04")],
         (2, 4), (4, 8), [(40, "ssd", "/")], ["nginx"], "frontend"),
        ("db", 9, "db-", "vm", [("centos", "7.9"), ("oracle linux", "8.6")],
         (8, 16), (32, 64), [(50, "ssd", "/"), (400, "ssd", "/data")], ["mysql"], "data"),
        ("cache", 3, "cache-", "vm", [("centos", "7.9")],
         (4, 4), (16, 32), [(40, "ssd", "/")], ["redis"], "data"),
        ("k8s-master", 2, "k8s-m-", "k8s-master", [("ubuntu", "22.04")],
         (8, 8), (16, 16), [(100, "ssd", "/")], ["k8s", "control-plane"], "infra"),
        ("k8s-worker", 6, "k8s-w-", "k8s-node", [("ubuntu", "22.04")],
         (16, 16), (64, 64), [(100, "ssd", "/"), (500, "ssd", "/var/lib/containerd")], ["k8s", "worker"], "infra"),
        ("hadoop", 6, "hdop-", "baremetal", [("centos", "7.9")],
         (16, 16), (64, 64), [(100, "ssd", "/"), (4000, "hdd", "/data1"), (4000, "hdd", "/data2")], ["hadoop"], "data"),
        ("monitoring", 2, "mon-", "vm", [("ubuntu", "22.04")],
         (2, 4), (4, 8), [(40, "ssd", "/")], ["grafana"], "infra"),
        ("general", 25, "srv-", "vm", [("centos", "7.9"), ("ubuntu", "22.04"), ("debian", "11"), ("windows", "2019")],
         (2, 8), (4, 16), [(50, "ssd", "/")], [], "backend"),
    ]
    weights = [s[1] for s in SPEC]
    envs = [("prod", 0.7), ("staging", 0.2), ("dev", 0.1)]
    dcs = [(("dc1", "10.10"), 0.6), (("dc2", "10.20"), 0.3), (("dc3", "10.30"), 0.1)]
    crits = [("high", 0.3), ("medium", 0.5), ("low", 0.2)]

    def pick(weighted):
        r = rnd.random(); cum = 0
        for v, p in weighted:
            cum += p
            if r <= cum:
                return v
        return weighted[-1][0]

    servers = []
    used_ips = set()
    for i in range(n):
        idx = rnd.choices(range(len(SPEC)), weights=weights, k=1)[0]
        role, _, prefix, stype, os_choices, cpu_r, mem_r, disk_spec, tags, tier = SPEC[idx]
        osn, osv = rnd.choice(os_choices)
        cpu = rnd.randint(*cpu_r)
        mem = rnd.randint(*mem_r)
        # round mem to common dims
        env = pick(envs)
        dc, net = pick(dcs)
        crit = pick(crits)
        sub = rnd.randint(1, 60)
        # unique IP
        while True:
            ip = f"{net}.{sub}.{rnd.randint(2, 250)}"
            if ip not in used_ips:
                used_ips.add(ip); break
        hn = f"{prefix}{i+1:05d}"
        fqdn = f"{hn}.{dc}.corp"
        disks = []
        for j, (gb, kind, fs) in enumerate(disk_spec):
            disks.append((f"sd{chr(97+j)}", gb + rnd.randint(0, 50) if j > 0 else gb, kind, fs))
        vlan = f"vlan{rnd.randint(10,90)}"
        cluster = f"cl-{role if role not in ('k8s-master','k8s-worker') else 'k8s'}"
        servers.append((hn, fqdn, ip, stype, role, osn, osv, cpu, mem, disks,
                        f"{net}.{sub}.0/24", vlan, dc, cluster, env, crit, [], list(tags)))

    # apps: bucket servers by role into apps of 8-20 servers; add deps for backend->data
    by_role = {}
    for s in servers:
        by_role.setdefault(s[4], []).append(s)
    apps = []
    app_of_server = {}
    for role, members in by_role.items():
        rnd.shuffle(members)
        tier = next(s[9] for s in SPEC if s[0] == role)
        for k in range(0, len(members), rnd.randint(8, 20)):
            chunk = members[k:k + rnd.randint(8, 20)]
            aid = f"app-{role}-{len(apps)+1}"
            hostnames = [c[0] for c in chunk]
            for hn in hostnames:
                app_of_server[hn] = aid
            apps.append((aid, f"{role} service {len(apps)+1}", tier, chunk[0][14], hostnames, [], ""))
    # attach app ids to servers
    servers = [(*s[:16], [app_of_server.get(s[0])] if app_of_server.get(s[0]) else [], s[17]) for s in servers]
    # dependencies: backend apps depend on a data app; frontend depend on a backend
    data_apps = [a[0] for a in apps if a[2] == "data"]
    back_apps = [a[0] for a in apps if a[2] == "backend"]
    apps = [list(a) for a in apps]
    for a in apps:
        if a[2] == "backend" and data_apps:
            a[5] = [rnd.choice(data_apps)]
        elif a[2] == "frontend" and back_apps:
            a[5] = rnd.sample(back_apps, min(2, len(back_apps)))
    apps = [tuple(a) for a in apps]
    SERVERS = servers
    APPS = apps


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=15, help="number of servers (15=curated demo; >15=synthetic)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--outdir", type=str, default=None, help="output dir (default: this fixtures dir)")
    a = ap.parse_args()
    if a.outdir:
        HERE = a.outdir
        os.makedirs(HERE, exist_ok=True)
    if a.count != 15:
        build_synthetic(a.count, seed=a.seed)
    write_servicenow()
    write_rvtools()
    write_zabbix()
    write_prometheus()
    write_apps()
    print(f"fixtures: {len(SERVERS)} servers, {len(APPS)} apps")