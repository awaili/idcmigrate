"""HTTP + store tests for the repo (git url) entity and the host↔repo N:N
mapping (Phase 1 of the app/host/repo redesign).

Runs against the real FastAPI app + the dedicated test DB (truncated at session
start by conftest), so skipped unless the app imports cleanly. Auth is off on a
fresh test DB (no web_password in system_config), so the session gate is open.

Isolation: the session conftest truncates everything once at start; within the
run we wipe the repo + edge tables AND the servers/workloads each test seeds
(using uuid-unique ids so other tests' servers can't collide via hostname
resolution).
"""
import uuid

import pytest

pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.core.models import Repo, Server, Workload

client = TestClient(m.app)


@pytest.fixture
def _clean():
    """Wipe repo/edge tables around each test and track+remove the
    servers/workloads the test seeds (uuid-unique ids avoid cross-test
    collisions in resolve_server_ids' hostname index)."""
    state = {"servers": [], "workloads": []}

    def _seed_server(hostname, app_ids=None):
        sid = f"srv-rtest-{uuid.uuid4().hex[:10]}"
        m.STORE.upsert_server(Server(id=sid, hostname=hostname,
                                     fqdn=f"{hostname}.dc",
                                     app_ids=app_ids or []))
        state["servers"].append(sid)
        return sid

    def _seed_workload(app_id, server_ids):
        m.STORE.upsert_workload(Workload(app_id=app_id, server_ids=list(server_ids)))
        state["workloads"].append(app_id)
        return app_id

    def _r():
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM host_repos"))
            cur.execute(m.STORE._x("DELETE FROM repos"))
    _r()
    yield {"server": _seed_server, "workload": _seed_workload}
    _r()
    for sid in state["servers"]:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM servers WHERE id=?"), (sid,))
    for app_id in state["workloads"]:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x("DELETE FROM workloads WHERE app_id=?"), (app_id,))


# -- repo entity -----------------------------------------------------------
def test_repo_create_list_update_delete(_clean):
    r = client.post("/api/repos", json={"url": "https://g.example/a.git", "branch": "main"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo_id"].startswith("repo-")
    assert body["name"] == "a"              # derived from url path, .git stripped
    lst = client.get("/api/repos").json()
    assert len(lst) == 1
    assert lst[0]["url"] == "https://g.example/a.git"
    assert lst[0]["name"] == "a" and lst[0]["branch"] == "main"
    assert lst[0]["host_count"] == 0 and lst[0]["hosts"] == [] and lst[0]["apps"] == []

    rid = body["repo_id"]
    u = client.put(f"/api/repos/{rid}", json={"url": "https://g.example/a.git",
                                              "branch": "dev", "name": "Alpha"})
    assert u.status_code == 200
    assert u.json()["branch"] == "dev" and u.json()["name"] == "Alpha"

    assert client.delete(f"/api/repos/{rid}").status_code == 200
    assert client.get("/api/repos").json() == []


def test_repo_url_unique_409(_clean):
    client.post("/api/repos", json={"url": "https://g.example/a.git"})
    r = client.post("/api/repos", json={"url": "https://g.example/a.git"})
    assert r.status_code == 409            # shared repo — link to hosts instead


def test_repo_rejects_bad_url(_clean):
    assert client.post("/api/repos", json={"url": "not a url"}).status_code == 400
    assert client.post("/api/repos", json={"url": ""}).status_code == 400


def test_repo_update_url_collision_409(_clean):
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    client.post("/api/repos", json={"url": "https://g.example/b.git"})
    r = client.put(f"/api/repos/{a}", json={"url": "https://g.example/b.git"})
    assert r.status_code == 409


# -- host ↔ repo N:N -------------------------------------------------------
def test_repo_hosts_n_n(_clean):
    seed = _clean
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    b = client.post("/api/repos", json={"url": "https://g.example/b.git"}).json()["repo_id"]
    s1 = seed["server"]("host1-rx")
    s2 = seed["server"]("host2-rx")

    # repo a on both hosts; repo b on host1 only -> host1 runs a+b, host2 runs a.
    r = client.put(f"/api/repos/{a}/hosts", json={"hosts": ["host1-rx", "host2-rx"]})
    assert r.status_code == 200
    assert r.json()["matched"] == 2 and r.json()["unresolved"] == []
    rb = client.put(f"/api/repos/{b}/hosts", json={"hosts": ["host1-rx", "nope"]})
    assert rb.json()["matched"] == 1 and rb.json()["unresolved"] == ["nope"]

    # repo a's hosts
    ha = client.get(f"/api/repos/{a}/hosts").json()
    assert sorted(h["hostname"] for h in ha) == ["host1-rx", "host2-rx"]

    # host side: host1 runs a+b, host2 runs a (N:N both directions)
    h1 = client.get(f"/api/hosts/{s1}/repos").json()
    h2 = client.get(f"/api/hosts/{s2}/repos").json()
    assert sorted(r2["repo_id"] for r2 in h1) == sorted([a, b])
    assert [r2["repo_id"] for r2 in h2] == [a]

    # enriched list carries counts
    lst = {r2["repo_id"]: r2 for r2 in client.get("/api/repos").json()}
    assert lst[a]["host_count"] == 2 and lst[b]["host_count"] == 1

    # PUT replaces the set (relink b to host2 only) and resolves by server_id
    client.put(f"/api/repos/{b}/hosts", json={"hosts": [s2]})
    assert sorted(h["hostname"] for h in client.get(f"/api/repos/{b}/hosts").json()) == ["host2-rx"]
    assert [r2["repo_id"] for r2 in client.get(f"/api/hosts/{s1}/repos").json()] == [a]


def test_app_repos_derived_from_hosts(_clean):
    seed = _clean
    s1 = seed["server"]("web-rx", app_ids=["APP-X"])
    s2 = seed["server"]("app-rx", app_ids=["APP-X"])
    seed["workload"]("APP-X", [s1, s2])
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    client.put(f"/api/repos/{a}/hosts", json={"hosts": ["web-rx", "app-rx"]})

    ar = client.get("/api/apps/APP-X/repos").json()
    assert [r2["repo_id"] for r2 in ar] == [a]            # derived, not stored
    lst = {r2["repo_id"]: r2 for r2 in client.get("/api/repos").json()}
    assert lst[a]["apps"] == ["APP-X"]

    # an app with no hosts mapped to this repo -> empty
    assert client.get("/api/apps/APP-Y/repos").json() == []


# -- app-centric sources (the Code tab primary view) -----------------------
def test_app_sources_enriched_shows_carrying_hosts(_clean):
    """GET /api/apps/{app_id}/sources = derived sources + which app hosts carry
    each, without loading the whole estate."""
    seed = _clean
    s1 = seed["server"]("web-rx", app_ids=["APP-A"])
    s2 = seed["server"]("app-rx", app_ids=["APP-A"])
    s3 = seed["server"]("db-rx", app_ids=["APP-A"])     # carries no repo
    seed["workload"]("APP-A", [s1, s2, s3])
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    b = client.post("/api/repos", json={"url": "https://g.example/b.git"}).json()["repo_id"]
    client.put(f"/api/repos/{a}/hosts", json={"hosts": ["web-rx", "app-rx"]})
    client.put(f"/api/repos/{b}/hosts", json={"hosts": ["app-rx"]})

    r = client.get("/api/apps/APP-A/sources").json()
    assert r["host_total"] == 3
    srcs = {s["repo_id"]: s for s in r["sources"]}
    assert sorted(srcs) == sorted([a, b])
    assert sorted(srcs[a]["hostnames"]) == ["app-rx", "web-rx"]
    assert srcs[a]["host_count"] == 2
    assert srcs[b]["hostnames"] == ["app-rx"]            # only app-rx carries b
    # an app-less repo (on a host not in this app) is NOT surfaced
    s4 = seed["server"]("other-rx")
    client.put(f"/api/repos/{a}/hosts", json={"hosts": ["other-rx", "web-rx"]})
    r2 = client.get("/api/apps/APP-A/sources").json()
    assert "other-rx" not in {s["repo_id"]: s for s in r2["sources"]}[a]["hostnames"]


def test_set_app_repos_add_remove_union_diff(_clean):
    """PUT /api/apps/{app_id}/repos links a repo to ALL the app's hosts (union,
    not replace — each host's other repos are preserved) and removes it (diff)."""
    seed = _clean
    s1 = seed["server"]("web-rx", app_ids=["APP-A"])
    s2 = seed["server"]("app-rx", app_ids=["APP-A"])
    seed["workload"]("APP-A", [s1, s2])
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    b = client.post("/api/repos", json={"url": "https://g.example/b.git"}).json()["repo_id"]
    # b already on s1 — adding a to the app must preserve b on s1
    client.put(f"/api/repos/{b}/hosts", json={"hosts": ["web-rx"]})

    r = client.put("/api/apps/APP-A/repos", json={"repo_id": a, "action": "add"})
    assert r.status_code == 200
    assert r.json()["hosts_changed"] == 2                 # a added to both s1 + s2
    # s1 still has b (union, not replace)
    s1repos = sorted(x["repo_id"] for x in client.get("/api/hosts/web-rx/repos").json())
    s2repos = sorted(x["repo_id"] for x in client.get("/api/hosts/app-rx/repos").json())
    assert s1repos == sorted([a, b])                      # b preserved on s1
    assert s2repos == [a]
    # the app now derives both a and b
    srcs = {s["repo_id"] for s in client.get("/api/apps/APP-A/sources").json()["sources"]}
    assert srcs == {a, b}

    # idempotent add: a already on all app hosts -> 0 changed, 2 skipped
    r2 = client.put("/api/apps/APP-A/repos", json={"repo_id": a, "action": "add"})
    assert r2.json()["hosts_changed"] == 0 and r2.json()["hosts_skipped"] == 2

    # remove a from the app -> diff, b preserved on s1
    r3 = client.put("/api/apps/APP-A/repos", json={"repo_id": a, "action": "remove"})
    assert r3.json()["hosts_changed"] == 2
    assert client.get("/api/hosts/web-rx/repos").json() and \
        sorted(x["repo_id"] for x in client.get("/api/hosts/web-rx/repos").json()) == [b]
    assert client.get("/api/hosts/app-rx/repos").json() == []
    srcs = {s["repo_id"] for s in client.get("/api/apps/APP-A/sources").json()["sources"]}
    assert srcs == {b}


def test_set_app_repos_404s(_clean):
    seed = _clean
    s1 = seed["server"]("h-rx", app_ids=["APP-A"])
    seed["workload"]("APP-A", [s1])
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    assert client.put("/api/apps/APP-NONE/repos", json={"repo_id": a}).status_code == 404
    assert client.put("/api/apps/APP-A/repos", json={"repo_id": "repo-nope"}).status_code == 404


def test_host_repos_set_and_resolve_by_hostname(_clean):
    """The host side of the N:N: PUT /api/hosts/{ident}/repos sets the source
    set for a host, and the {ident} path token resolves by hostname/fqdn/id."""
    seed = _clean
    s1 = seed["server"]("web-rx")
    a = client.post("/api/repos", json={"url": "https://g.example/a.git"}).json()["repo_id"]
    b = client.post("/api/repos", json={"url": "https://g.example/b.git"}).json()["repo_id"]

    # PUT by hostname (not server_id) sets the host's source set
    r = client.put("/api/hosts/web-rx/repos", json={"repo_ids": [a, b]})
    assert r.status_code == 200 and r.json()["linked"] == 2 and r.json()["unresolved"] == []
    # GET by hostname returns both repos
    got = client.get("/api/hosts/web-rx/repos").json()
    assert sorted(r2["repo_id"] for r2 in got) == sorted([a, b])
    # GET by server_id returns the same (token resolution)
    assert sorted(r2["repo_id"] for r2 in client.get(f"/api/hosts/{s1}/repos").json()) == sorted([a, b])
    # GET by fqdn too
    assert len(client.get("/api/hosts/web-rx.dc/repos").json()) == 2

    # an unknown repo id in the set is reported, not linked
    r2 = client.put("/api/hosts/web-rx/repos", json={"repo_ids": [a, "repo-nope"]})
    assert r2.json()["linked"] == 1 and r2.json()["unresolved"] == ["repo-nope"]
    assert [r3["repo_id"] for r3 in client.get("/api/hosts/web-rx/repos").json()] == [a]

    # unknown host -> 404
    assert client.put("/api/hosts/ghost/repos", json={"repo_ids": [a]}).status_code == 404
    assert client.get("/api/hosts/ghost/repos").status_code == 404


# -- store-level edge mechanics -------------------------------------------
def test_store_n_n_replace_and_cascade(_clean):
    seed = _clean
    s1 = seed["server"]("h1")
    s2 = seed["server"]("h2")
    ra = Repo(repo_id="repo-a", url="https://g.example/a.git", name="a")
    rb = Repo(repo_id="repo-b", url="https://g.example/b.git", name="b")
    m.STORE.upsert_repo(ra); m.STORE.upsert_repo(rb)

    # repo-a on s1+s2 (a bogus id is stored as an edge but filtered on join reads)
    m.STORE.set_repo_hosts("repo-a", [s1, s2, "ghost"])
    assert sorted(m.STORE.hosts_for_repo("repo-a")) == sorted([s1, s2])
    assert m.STORE.host_repos_for(s1) == ["repo-a"]

    # set_host_repos replaces the host's repo set
    m.STORE.set_host_repos(s1, ["repo-b"])
    assert m.STORE.host_repos_for(s1) == ["repo-b"]
    assert m.STORE.hosts_for_repo("repo-a") == [s2]      # s1 dropped from repo-a

    # delete_repo cascades its edges
    assert m.STORE.delete_repo("repo-b") is True
    assert m.STORE.get_repo("repo-b") is None
    assert m.STORE.host_repos_for(s1) == []               # edge to repo-b gone
    assert m.STORE.delete_repo("repo-b") is False         # already gone -> 404 path


def test_store_repos_for_app_derived(_clean):
    seed = _clean
    s1 = seed["server"]("h1")
    s2 = seed["server"]("h2")
    seed["workload"]("APP-Z", [s1, s2])
    m.STORE.upsert_repo(Repo(repo_id="repo-a", url="https://g.example/a.git"))
    m.STORE.set_repo_hosts("repo-a", [s1, s2])
    assert m.STORE.repos_for_app("APP-Z") == ["repo-a"]
    assert m.STORE.repos_for_app("APP-NONE") == []


def test_resolve_server_ids_by_hostname_fqdn_id(_clean):
    seed = _clean
    sid = seed["server"]("web-01")
    res = m.STORE.resolve_server_ids(["web-01", "web-01.dc", sid, "nope", ""])
    # all three real tokens resolve to the same id and are de-duped; "" / "nope" excluded
    assert res["matched"] == [sid]
    assert res["unresolved"] == ["nope"]
    assert m.STORE.resolve_server_ids([]) == {"matched": [], "unresolved": []}


# -- host typeahead (/api/hosts/suggest for the chips editor + host picker) --
def test_host_suggest_matches_substring(_clean):
    """GET /api/hosts/suggest?q=<substr> returns [{server_id,hostname,fqdn,role}]
    matching hostname/fqdn, no per-row enrichment, capped by limit."""
    seed = _clean
    seed["server"]("web-payroll-01")
    seed["server"]("web-payroll-02")
    seed["server"]("app-orders-07")
    r = client.get("/api/hosts/suggest?q=payroll").json()
    names = sorted(h["hostname"] for h in r)
    assert names == ["web-payroll-01", "web-payroll-02"]   # substring match
    assert all(set(h) == {"server_id", "hostname", "fqdn", "role"} for h in r)

    # empty q -> first `limit` by hostname (no crash, bounded)
    r0 = client.get("/api/hosts/suggest").json()
    assert isinstance(r0, list) and len(r0) <= 50

    # limit cap respected
    rl = client.get("/api/hosts/suggest?limit=1").json()
    assert len(rl) <= 1

    # no match -> empty list (not 404)
    assert client.get("/api/hosts/suggest?q=zzzznomatch").json() == []