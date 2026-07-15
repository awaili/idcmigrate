"""Diagnostic: run every LZ-page starter scenario × its scale tier through the
REAL LLM (LLMClient.design_lz) over the live estate and check the returned
design meets per-scenario expectations (validates, scale stamped, governance
filled, app_map honored, topology shape right). Not a pytest — it hits the live
LLM and is slow/flaky; run it directly:

    IDC_DB_URL='mysql://...idc_migrate' python scripts/test_lz_scenarios.py [--workers N]

Prints a pass/fail table + the failing details. Used to verify the scale-aware
LZ prompt/validator/engine design end-to-end.
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from idc.config import get_settings
from idc.core.db import Store
from idc.core.lz import archetype_for
from idc.core.models import Server
from idc.llm.client import LLMClient

# dominant role per app id used in expectations — so the `apps` check can resolve
# the REAL classification (app_map OR role_map fallback) via archetype_for, not
# just a raw app_map lookup (the LLM may route DB apps via role_map db->data and
# only put the explicitly-named apps in app_map — still correct placement).
APP_ROLE = {
    "APP_0001": "db", "APP_0020": "db", "APP_0530": "db", "APP_0156": "db",
    "APP_0069": "db", "APP_0087": "db", "APP_0508": "db",
    "APP_0073": "web", "APP_0468": "web", "APP_0030": "web",
    "APP_0455": "hadoop", "APP_0006": "hadoop",
    "APP_0234": "general", "APP_0581": "general",
    "APP_0374": "general", "APP_0335": "general", "APP_0610": "general",
    "APP_0147": "general", "APP_0573": "general",
    "APP_0278": "general", "APP_0260": "general",
}

# (label, scale, prompt, expectations)
# expectations:
#   apps: {app_id: archetype} that MUST appear in classifier.app_map
#   must_include: archetype names that must be present
#   must_exclude: archetype names that must NOT be present
#   corp_cidr: expected corp VPC cidr
#   no_internet: every archetype internet_egress all-false
#   cfw_scp: at least one policy_as_code mentions Cloud Firewall or SCP
SCENARIOS = [
    ("Flat — assign apps", "small",
     'Small estate — KEEP the flat 2-VPC default (corp + online, NO dmz tier, NO hub transit). Only assign apps: public web apps APP_0073, APP_0468, APP_0030 -> "online"; DB/internal apps APP_0001, APP_0020 -> "corp". role_map: web->online, db->corp; default "corp". Do NOT add a dmz tier or hub-and-spoke — the small default is flat.',
     {"apps": {"APP_0073": "online", "APP_0468": "online", "APP_0001": "corp"},
      "must_include": {"online", "corp"}, "must_exclude": {"dmz"}}),
    ("Single account", "small",
     'Smallest footprint — collapse to ONE workload account: a single VPC "corp" (172.16.0.0/16) with DC peering + NAT, a public CLB for the web apps in the same VPC. app_map APP_0001, APP_0073 -> "corp"; default "corp". Drop the separate online VPC.',
     {"apps": {"APP_0001": "corp", "APP_0073": "corp"}, "must_include": {"corp"}}),
    ("Promote app to online", "small",
     'Iterate: expose APP_0468 (currently in "corp") to the internet by moving it to "online". app_map APP_0468 -> "online". Keep the flat 2-VPC layout and the other assignments unchanged.',
     {"apps": {"APP_0468": "online"}, "must_include": {"online", "corp"},
      "must_exclude": {"dmz"}}),
    ("Data + Online + Hub", "medium",
     'On top of the hub-and-spoke default (corp hub + online + dmz), assign apps to workload accounts by shape:\n- "data": no internet egress, DC via the hub -> DB apps APP_0001, APP_0020, APP_0530, APP_0156, APP_0069, APP_0087, APP_0508 (-> TencentDB/TData).\n- "online": public CLB + CDN + WAF -> web/app apps APP_0073, APP_0468, APP_0030.\n- "corp" hub: infra/mgmt APP_0278, APP_0260 + the catch-all.\nrole_map db->data, web->online; default corp. Spokes reach the DC via corp only (no direct spoke->DC).',
     {"apps": {"APP_0001": "data", "APP_0073": "online", "APP_0278": "corp"},
      "must_include": {"corp", "online", "data"}}),
    ("+ Big-data account", "medium",
     'Add a big-data account to the hub-and-spoke: "bigdata" (no internet egress, Tencent EMR + CFS), peers only to corp for data ingest + the on-prem data lake. app_map APP_0455 (hadoop/spark), APP_0006 (spark/kafka) -> "bigdata". Keep data (APP_0001, APP_0020, APP_0530), online (APP_0073, APP_0468), corp hub as before.',
     {"apps": {"APP_0455": "bigdata", "APP_0006": "bigdata", "APP_0001": "data",
               "APP_0073": "online"},
      "must_include": {"bigdata", "data", "online", "corp"}}),
    ("+ Storage account", "medium",
     'Add a storage account: "storage" (no internet egress, self-hosted object/file -> COS + CFS), peers only to corp. app_map APP_0234 (minio), APP_0581 (gluster) -> "storage". Keep data/online/corp as before.',
     {"apps": {"APP_0234": "storage", "APP_0581": "storage", "APP_0001": "data"},
      "must_include": {"storage", "data", "online", "corp"}}),
    ("+ Middleware account", "medium",
     'Split the middleware runtimes into their own account: "middleware" (no internet egress -> TKE/TDMQ/TSE), peers only to corp. app_map APP_0374 (zk), APP_0335 (weblogic), APP_0610 (es), APP_0147 (rabbitmq), APP_0573 (websphere) -> "middleware". Keep data/online/corp as before.',
     {"apps": {"APP_0374": "middleware", "APP_0335": "middleware",
               "APP_0001": "data"},
      "must_include": {"middleware", "data", "online", "corp"}}),
    ("Regulated / PCI", "medium",
     'Add a PCI-isolated account to the hub-and-spoke: "pci" (NO internet egress, no public CLB, CAM scoped to PCI operators, mandatory encryption, tag_policy env=pci), peers only to corp — NEVER to online/data directly. app_map APP_0156, APP_0069 -> "pci". Keep online (APP_0073, APP_0468), data (APP_0001, APP_0020, APP_0530), corp hub.',
     {"apps": {"APP_0156": "pci", "APP_0069": "pci", "APP_0073": "online",
               "APP_0001": "data"},
      "must_include": {"pci", "online", "data", "corp"}}),
    ("Move app between accounts", "medium",
     'Iterate: move APP_0087 (a postgres app currently in "data") into "online" so its app tier sits with the public apps. app_map APP_0087 -> "online" (overrides role_map db->data for this one app). Keep APP_0001, APP_0020, APP_0530 -> "data" and the corp hub unchanged.',
     {"apps": {"APP_0087": "online", "APP_0001": "data"},
      "must_include": {"online", "data"}}),
    ("Shrink hub CIDR", "medium",
     'Iterate: change the hub ("corp") VPC CIDR to 10.8.0.0/16 and re-check that no account VPC overlaps it or the on-prem ranges. Do not change any app_map assignment.',
     {"corp_cidr": "10.8.0.0/16", "must_include": {"corp"}}),
    ("Data + Online + Hub", "large",
     'On top of the hub-and-spoke default (corp hub + online + dmz), assign apps to workload accounts by shape:\n- "data": no internet egress, DC via the hub -> DB apps APP_0001, APP_0020, APP_0530, APP_0156, APP_0069, APP_0087, APP_0508 (-> TencentDB/TData).\n- "online": public CLB + CDN + WAF -> web/app apps APP_0073, APP_0468, APP_0030.\n- "corp" hub: infra/mgmt APP_0278, APP_0260 + the catch-all.\nrole_map db->data, web->online; default corp. Spokes reach the DC via corp only (no direct spoke->DC).',
     {"apps": {"APP_0001": "data", "APP_0073": "online"},
      "must_include": {"corp", "online", "data"}, "cfw_scp": True}),
    ("+ Big-data account", "large",
     'Add a big-data account to the hub-and-spoke: "bigdata" (no internet egress, Tencent EMR + CFS), peers only to corp for data ingest + the on-prem data lake. app_map APP_0455 (hadoop/spark), APP_0006 (spark/kafka) -> "bigdata". Keep data (APP_0001, APP_0020, APP_0530), online (APP_0073, APP_0468), corp hub as before.',
     {"apps": {"APP_0455": "bigdata", "APP_0001": "data"},
      "must_include": {"bigdata", "data", "online", "corp"}, "cfw_scp": True}),
    ("+ Storage account", "large",
     'Add a storage account: "storage" (no internet egress, self-hosted object/file -> COS + CFS), peers only to corp. app_map APP_0234 (minio), APP_0581 (gluster) -> "storage". Keep data/online/corp as before.',
     {"apps": {"APP_0234": "storage", "APP_0001": "data"},
      "must_include": {"storage", "data", "online", "corp"}, "cfw_scp": True}),
    ("+ Middleware account", "large",
     'Split the middleware runtimes into their own account: "middleware" (no internet egress -> TKE/TDMQ/TSE), peers only to corp. app_map APP_0374 (zk), APP_0335 (weblogic), APP_0610 (es), APP_0147 (rabbitmq), APP_0573 (websphere) -> "middleware". Keep data/online/corp as before.',
     {"apps": {"APP_0374": "middleware", "APP_0001": "data"},
      "must_include": {"middleware", "data", "online", "corp"}, "cfw_scp": True}),
    ("Regulated / PCI", "large",
     'Add a PCI-isolated account to the hub-and-spoke: "pci" (NO internet egress, no public CLB, CAM scoped to PCI operators, mandatory encryption, tag_policy env=pci), peers only to corp — NEVER to online/data directly. app_map APP_0156, APP_0069 -> "pci". Keep online (APP_0073, APP_0468), data (APP_0001, APP_0020, APP_0530), corp hub.',
     {"apps": {"APP_0156": "pci", "APP_0001": "data"},
      "must_include": {"pci", "online", "data", "corp"}, "cfw_scp": True}),
    ("Move app between accounts", "large",
     'Iterate: move APP_0087 (a postgres app currently in "data") into "online" so its app tier sits with the public apps. app_map APP_0087 -> "online" (overrides role_map db->data for this one app). Keep APP_0001, APP_0020, APP_0530 -> "data" and the corp hub unchanged.',
     {"apps": {"APP_0087": "online", "APP_0001": "data"},
      "must_include": {"online", "data"}, "cfw_scp": True}),
    ("Shrink hub CIDR", "large",
     'Iterate: change the hub ("corp") VPC CIDR to 10.8.0.0/16 and re-check that no account VPC overlaps it or the on-prem ranges. Do not change any app_map assignment.',
     {"corp_cidr": "10.8.0.0/16", "must_include": {"corp"}, "cfw_scp": True}),
    ("Enterprise + CFW/SCP", "large",
     'Large enterprise — KEEP the large-tier defaults (hub-and-spoke + Cloud Firewall inspection on all inter-VPC traffic, SCP guardrails, centralized ActionTrail in a logging account). Split workloads across accounts by shape: data (APP_0001, APP_0020, APP_0530), online (APP_0073, APP_0468, APP_0030), bigdata (APP_0455, APP_0006), storage (APP_0234, APP_0581), middleware (APP_0374, APP_0335, APP_0610), corp hub. Every spoke peers only to corp; CFW inspects east-west traffic.',
     {"apps": {"APP_0001": "data", "APP_0073": "online", "APP_0455": "bigdata",
               "APP_0234": "storage", "APP_0374": "middleware"},
      "must_include": {"corp", "data", "online", "bigdata", "storage", "middleware"},
      "cfw_scp": True}),
    ("Air-gapped enterprise", "large",
     'Air-gapped regulated enterprise — NO internet egress anywhere. Keep the large-tier governance (SCP guardrails, centralized ActionTrail, encryption) but drop NAT/public CLB/CDN/WAF. Accounts: data (APP_0001, APP_0020, APP_0530, APP_0156), app (APP_0073, APP_0468, APP_0030), corp hub (DC peering only). All peer only via corp.',
     {"apps": {"APP_0001": "data", "APP_0073": "app"},
      "must_include": {"corp", "data", "app"}, "no_internet": True, "cfw_scp": True}),
]

BAND = {"small": (1, 3), "medium": (3, 5), "large": (3, 7)}


def _policy_has_cfw(d):
    for bp in (d.archetypes or {}).values():
        for p in (bp.get("policy_as_code") or []):
            low = p.lower()
            if "cloud firewall" in low or "cfw" in low or "scp" in low:
                return True
    return False


def _all_egress_off(d):
    for bp in (d.archetypes or {}).values():
        eg = bp.get("internet_egress") or {}
        if any(eg.get(k) for k in ("nat", "public_clb", "cdn", "waf")):
            return False
    return True


def check(label, scale, exp, r):
    d = r.get("design")
    fails = []
    if not r["ok"]:
        fails.append(f"ok=False ({r.get('error') or r.get('validation',{}).get('errors')})")
        return fails, d
    if not d:
        return ["no design returned"], d
    if getattr(d, "scale", "") != scale:
        fails.append(f"scale={d.scale!r} expected {scale!r}")
    archs = set(d.archetypes or {})
    lo, hi = BAND[scale]
    if not (lo <= len(archs) <= hi):
        fails.append(f"archetype count {len(archs)} not in [{lo},{hi}] (got {sorted(archs)})")
    gov = (d.requirements or {}).get("governance") or {}
    if not (gov.get("identity") and gov.get("audit") and gov.get("network")):
        fails.append("governance missing identity/audit/network")
    clf = (d.requirements or {}).get("classifier") or {}
    am = clf.get("app_map") or {}
    for app, arch in (exp.get("apps") or {}).items():
        # resolve the REAL placement the gate/engine would use: a server hosting
        # this app (with its dominant role) classified by the design's classifier
        # (app_map wins, else role_map, else default). The LLM may legitimately
        # route via role_map (e.g. db->data) without an explicit app_map entry —
        # that is still a correct placement, so check classification not app_map.
        s = Server(id="t", hostname="t", role=APP_ROLE.get(app, "general"),
                   app_ids=[app])
        got = archetype_for(s, None, clf)
        if got != arch:
            fails.append(f"app {app} classifies->{got!r} expected {arch!r} "
                         f"(app_map={am.get(app)!r}, role_map={clf.get('role_map')})")
    for a in (exp.get("must_include") or set()):
        if a not in archs:
            fails.append(f"missing archetype {a!r} (got {sorted(archs)})")
    for a in (exp.get("must_exclude") or set()):
        if a in archs:
            fails.append(f"should NOT have archetype {a!r} (got {sorted(archs)})")
    if "corp_cidr" in exp:
        cc = ((d.archetypes or {}).get("corp", {}).get("vpc") or {}).get("cidr")
        if cc != exp["corp_cidr"]:
            fails.append(f"corp cidr={cc!r} expected {exp['corp_cidr']!r}")
    if exp.get("no_internet") and not _all_egress_off(d):
        fails.append("air-gapped: some archetype has internet egress on")
    if exp.get("cfw_scp") and not _policy_has_cfw(d):
        fails.append("large: no CFW/SCP in any policy_as_code")
    # hard validation errors (warnings are fine)
    verrs = (r.get("validation") or {}).get("errors") or []
    if verrs:
        fails.append(f"validation errors: {verrs}")
    return fails, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="run only first N combos")
    ap.add_argument("--only", type=str, default="", help="comma substrings; run only matching labels")
    args = ap.parse_args()
    settings = get_settings()
    st = Store(settings.db_url)
    servers = st.list_all_servers()
    st.close()
    print(f"loaded {len(servers)} servers; llm={settings.llm_base} model={settings.llm_model}")
    combos = SCENARIOS[:args.limit] if args.limit else SCENARIOS
    if args.only:
        subs = [s.strip() for s in args.only.split(",") if s.strip()]
        combos = [c for c in combos if any(s in c[0] for s in subs)]
    print(f"running {len(combos)} scenario×scale combos with {args.workers} workers\n")

    def run_one(i, combo):
        label, scale, prompt, exp = combo
        llm = LLMClient(get_settings())   # fresh client per thread
        t = time.time()
        try:
            r = llm.design_lz(prompt, servers, scale=scale, max_rounds=2)
        except Exception as e:
            r = {"ok": False, "error": repr(e), "rounds": [], "validation": None,
                 "design": None}
        dt = time.time() - t
        fails, d = check(label, scale, exp, r)
        return (i, label, scale, dt, fails, d, r)

    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run_one, i, c) for i, c in enumerate(combos)]
        for f in as_completed(futs):
            i, label, scale, dt, fails, d, r = f.result()
            results[i] = (label, scale, dt, fails, d, r)
            mark = "✓" if not fails else "✗"
            print(f"{mark} [{scale:6}] {label:28} {dt:5.1f}s  {'OK' if not fails else 'FAIL'}")

    print("\n=== DETAILS (failures) ===")
    npass = 0
    for i in range(len(combos)):
        label, scale, dt, fails, d, r = results[i]
        if not fails:
            npass += 1
            continue
        print(f"\n✗ [{scale}] {label}  ({dt:.1f}s, rounds={len(r.get('rounds',[]))})")
        for f in fails:
            print(f"   - {f}")
        archs = list((d.archetypes or {})) if d else []
        am = (((d.requirements or {}).get("classifier") or {}).get("app_map")) if d else None
        print(f"   archs={archs}")
        if am:
            print(f"   app_map={dict(list(am.items())[:10])}")

    print(f"\n=== {npass}/{len(combos)} combos passed ===")
    sys.exit(0 if npass == len(combos) else 1)


if __name__ == "__main__":
    main()