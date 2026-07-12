"""Tests for codeintel + plan_waves enrichment + CodeProfile serialization.

Pure — no DB, no network.
"""
import pytest

from idc.core.codeintel import (
    app_targets,
    build_change_spec,
    enrich_match,
    index_profiles,
    merge_code_deps,
    order_apps_for_waves,
    question_for_change,
    resolve_value,
)
from idc.core.models import (
    CodeProfile,
    Match,
    PATTERN_REWRITE,
    Question,
    ScanFinding,
    Server,
    Target,
    Workload,
)
from idc.core.plan import plan_waves


def _profile(app_id, **kw):
    base = dict(app_id=app_id, cloud_readiness=0.8, migration_pattern="rehost",
                refactor_effort="low", code_deps=[], blockers=[],
                findings=[], required_changes=[])
    base.update(kw)
    return CodeProfile(**base)


def test_codeprofile_roundtrip():
    p = _profile("orders", findings=[ScanFinding(category="hardcoded_ip",
                severity="high", file="a.py", line=3, message="x")],
                code_deps=["users"], blockers=["b1"])
    d = p.to_dict()
    p2 = CodeProfile.from_dict(d)
    assert p2.app_id == "orders"
    assert p2.code_deps == ["users"]
    assert p2.blockers == ["b1"]
    assert p2.findings[0].category == "hardcoded_ip"
    assert p2.findings[0].line == 3


def test_codeprofile_from_dict_is_idempotent_with_prefindings():
    # the store pre-deserializes findings into ScanFinding objects then calls
    # from_dict — that must not choke trying to .get() on an already-built object
    f = ScanFinding(category="db_connection", file="a", line=1, message="m")
    p = CodeProfile.from_dict({"app_id": "orders", "findings": [f],
                                "code_deps": ["x"]})
    assert p.findings[0] is f or p.findings[0].category == "db_connection"
    assert p.code_deps == ["x"]


def test_merge_code_deps_adds_edges_dedup():
    wls = [Workload(app_id="orders", depends_on=["inventory"]),
           Workload(app_id="inventory"), Workload(app_id="users")]
    profs = [_profile("orders", code_deps=["users", "inventory"]),  # inventory already in deps
             _profile("inventory", code_deps=["users"])]
    merge_code_deps(wls, profs)
    by = {w.app_id: w for w in wls}
    assert "users" in by["orders"].depends_on          # added
    assert by["orders"].depends_on.count("inventory") == 1  # dedup (no double)
    assert "users" in by["inventory"].depends_on


def test_merge_code_deps_ignores_unknown_and_self():
    wls = [Workload(app_id="orders", depends_on=[])]
    profs = [_profile("orders", code_deps=["orders", "ghost-app"])]  # self + unknown
    merge_code_deps(wls, profs)
    assert wls[0].depends_on == []


def test_order_apps_cheaper_first():
    pidx = index_profiles([
        _profile("a", refactor_effort="high", migration_pattern="refactor"),
        _profile("b", refactor_effort="low", migration_pattern="rehost"),
        _profile("c", refactor_effort="medium", migration_pattern="replatform"),
    ])
    order = order_apps_for_waves(["a", "b", "c"], pidx)
    assert order == ["b", "c", "a"]   # low/rehost → medium/replatform → high/refactor


def test_plan_waves_uses_code_deps_and_order():
    # three app servers + a db; apps: orders depends-on inventory (via code dep)
    servers_in = [
        Server(hostname="db-mysql-01", role="db", app_ids=[]),
        Server(hostname="app-orders-01", role="app", app_ids=["orders"]),
        Server(hostname="app-inv-01", role="app", app_ids=["inventory"]),
        Server(hostname="app-cheap-01", role="app", app_ids=["cheap"]),
    ]
    wls = [
        Workload(app_id="orders", tier="backend", server_ids=["app-orders-01"],
                 depends_on=[]),   # NO static dep — code must supply it
        Workload(app_id="inventory", tier="backend", server_ids=["app-inv-01"],
                 depends_on=[]),
        Workload(app_id="cheap", tier="backend", server_ids=["app-cheap-01"],
                 depends_on=[]),
    ]
    profs = [
        _profile("orders", code_deps=["inventory"], refactor_effort="high",
                 migration_pattern="refactor"),
        _profile("inventory", refactor_effort="low", migration_pattern="rehost"),
        _profile("cheap", refactor_effort="low", migration_pattern="rehost"),
    ]
    waves = plan_waves(servers_in, wls, code_profiles=profs)
    host_by_id = {s.id: s.hostname for s in servers_in}
    def wave_for(host):
        return next(w for w in waves if any(host_by_id.get(sid) == host for sid in w.server_ids))
    inv_wave = wave_for("app-inv-01")
    ord_wave = wave_for("app-orders-01")
    # orders must depend on inventory's wave (edge came from code_deps)
    assert inv_wave.id in ord_wave.depends_on
    # cheap (low effort) should be scheduled before orders (high effort):
    # i.e. cheap's wave appears earlier in the list than orders' wave
    cheap_wave = wave_for("app-cheap-01")
    assert waves.index(cheap_wave) < waves.index(ord_wave)
    # rationale carries code-intel annotation
    assert "[pattern=refactor" in ord_wave.rationale


def test_enrich_match_blockers_lower_confidence_and_annotate():
    m = Match(server_id="srv-1", target=Target(product="CVM"), confidence=0.9,
              rationale="rule: app→CVM")
    p = _profile("orders", cloud_readiness=0.3, blockers=["Quartz on crontab"])
    enrich_match(m, p)
    assert m.confidence < 0.9
    assert "Quartz on crontab" in m.rationale
    assert "[code]" in m.rationale


def test_enrich_match_rewrite_floors_confidence():
    m = Match(server_id="srv-1", target=Target(product="CVM"), confidence=0.95,
              rationale="r")
    enrich_match(m, _profile("orders", migration_pattern=PATTERN_REWRITE))
    assert m.confidence <= 0.2
    assert "rewrite" in m.rationale


def test_enrich_match_none_is_noop():
    m = Match(server_id="srv-1", target=Target(product="CVM"), confidence=0.7,
              rationale="r")
    enrich_match(m, None)
    assert m.confidence == 0.7 and m.rationale == "r"


def test_enrich_match_high_readiness_no_blocker_rehost_no_penalty():
    m = Match(server_id="srv-1", target=Target(product="CVM"), confidence=0.8,
              rationale="r")
    p = _profile("orders", cloud_readiness=0.9, migration_pattern="rehost",
                 blockers=[])
    enrich_match(m, p)
    assert m.confidence == 0.8   # no penalty


# ---------------------------------------------------------------------------
# build_change_spec — the concrete "what to change" for modify requests
# ---------------------------------------------------------------------------
def _finding(**kw):
    base = dict(category="db_connection", severity="high", file="",
                line=0, message="", evidence="", remediation="")
    base.update(kw)
    return ScanFinding(**base)


def test_build_change_spec_no_profile_tells_executor_to_scan():
    spec = build_change_spec(None)
    assert spec.changes == []
    assert any("no CodeProfile" in n for n in spec.notes)


def test_build_change_spec_joins_required_changes_with_findings():
    p = _profile(
        "orders",
        required_changes=[
            {"title": "Parameterize DB host", "category": "db_connection",
             "file": "src/main/resources/application.yml", "line": 12,
             "effort": "low",
             "description": "Replace hardcoded 10.0.4.20 with ${DB_HOST}"},
        ],
        findings=[
            _finding(category="db_connection", file="src/main/resources/application.yml",
                     line=12, message="hardcoded DB host",
                     evidence="url: jdbc:mysql://10.0.4.20:3306/orders",
                     remediation="use ${DB_HOST} from env"),
        ])
    spec = build_change_spec(p)
    assert spec.app_id == "orders"
    assert len(spec.changes) == 1
    c = spec.changes[0]
    assert c["file"] == "src/main/resources/application.yml"
    assert c["line"] == 12
    assert c["old"] == "10.0.4.20:3306"          # extracted from evidence
    assert c["new"] == "${DB_HOST}"              # default placeholder
    assert c["kind"] == "endpoint"
    assert "jdbc:mysql://10.0.4.20:3306/orders" in c["evidence"]


def test_build_change_spec_scope_filters_categories():
    p = _profile(
        "orders",
        required_changes=[
            {"title": "DB host", "category": "db_connection", "file": "a", "line": 1},
            {"title": "Eureka", "category": "service_discovery", "file": "b", "line": 2},
        ],
        findings=[
            _finding(category="db_connection", file="a", line=1,
                     evidence="10.0.4.20:3306"),
            _finding(category="service_discovery", file="b", line=2,
                     evidence="eureka.dc1:8761"),
        ])
    spec = build_change_spec(p, scope=["db_connection"])
    assert [c["category"] for c in spec.changes] == ["db_connection"]


def test_build_change_spec_operator_overrides_pin_new_values():
    p = _profile(
        "orders",
        required_changes=[
            {"title": "DB host", "category": "db_connection", "file": "a", "line": 1},
            {"title": "DB password", "category": "secrets_in_repo", "file": "a", "line": 2},
        ],
        findings=[
            _finding(category="db_connection", file="a", line=1,
                     evidence="10.0.4.20:3306"),
            _finding(category="secrets_in_repo", file="a", line=2,
                     evidence="password: hunter2"),
        ])
    spec = build_change_spec(p, overrides={
        "10.0.4.20:3306": "${ORDERS_DB_HOST}:3306",
        "hunter2": "${ORDERS_DB_PASSWORD}",
    })
    by_cat = {c["category"]: c for c in spec.changes}
    assert by_cat["db_connection"]["new"] == "${ORDERS_DB_HOST}:3306"
    assert by_cat["secrets_in_repo"]["new"] == "${ORDERS_DB_PASSWORD}"
    assert by_cat["secrets_in_repo"]["old"] == "hunter2"   # extracted for find&replace


def test_build_change_spec_unmatched_override_reported_in_notes():
    p = _profile("orders",
                 required_changes=[
                     {"title": "DB host", "category": "db_connection",
                      "file": "a", "line": 1}],
                 findings=[_finding(category="db_connection", file="a", line=1,
                                    evidence="10.0.4.20:3306")])
    spec = build_change_spec(p, overrides={"not-in-repo": "${X}"})
    assert any("ignored" in n for n in spec.notes)


def test_build_change_spec_secret_with_no_extractable_old_is_redacted():
    p = _profile("orders",
                 required_changes=[
                     {"title": "API key", "category": "secrets_in_repo",
                      "file": "a", "line": 5}],
                 findings=[_finding(category="secrets_in_repo", file="a", line=5,
                                    evidence="// TODO rotate this", message="api key")])
    spec = build_change_spec(p)
    c = spec.changes[0]
    assert c["old"] == ""                       # not leaked as a guess
    assert "redact" in c["note"] or "locate" in c["note"]


def test_build_change_spec_finding_not_in_required_changes_still_emitted():
    # a hardcoded_ip finding the comb step didn't fold into a required_change
    p = _profile("orders", required_changes=[],
                 findings=[_finding(category="hardcoded_ip", file="a", line=1,
                                    evidence="10.0.0.5", message="ip")])
    spec = build_change_spec(p)
    assert len(spec.changes) == 1
    assert spec.changes[0]["old"] == "10.0.0.5"
    assert spec.changes[0]["new"] == "${HOST}"


def test_build_change_spec_empty_profile_says_nothing_to_change():
    spec = build_change_spec(_profile("orders"))
    assert spec.changes == []
    assert any("nothing concrete" in n for n in spec.notes)


# ---------------------------------------------------------------------------
# ongoing interaction — app_targets + resolve_value
# ---------------------------------------------------------------------------
def test_app_targets_returns_matches_for_app_servers():
    wls = [Workload(app_id="orders", server_ids=["srv-1", "srv-2"]),
           Workload(app_id="users", server_ids=["srv-3"])]
    matches = [
        Match(server_id="srv-1", target=Target(product="CDB"), confidence=0.9),
        Match(server_id="srv-2", target=Target(product="CVM"), confidence=0.8),
        Match(server_id="srv-3", target=Target(product="CVM"), confidence=0.7),
    ]
    out = app_targets(wls, matches, "orders")
    assert {m.server_id for m in out} == {"srv-1", "srv-2"}
    assert any(m.target.product == "CDB" for m in out)


def test_app_targets_unknown_app_is_empty():
    assert app_targets([Workload(app_id="x")], [], "ghost") == []


def test_resolve_value_override_wins_high_confidence():
    r = resolve_value("ip", "10.0.4.20",
                      overrides={"10.0.4.20": "${ORDERS_DB_HOST}"})
    assert r["new"] == "${ORDERS_DB_HOST}"
    assert r["source"] == "override"
    assert r["confidence"] == 1.0


def test_resolve_value_match_derives_db_host_placeholder():
    matches = [Match(server_id="s", target=Target(product="CDB"), confidence=0.9)]
    r = resolve_value("endpoint", "10.0.4.20:3306", matches=matches)
    assert r["new"] == "${DB_HOST}"               # refined by CDB match
    assert r["source"] == "match-derived"
    assert 0 < r["confidence"] < 1               # a suggestion, not a fact


def test_resolve_value_unknown_kind_falls_back_to_generic():
    r = resolve_value("config", "weird-token")
    assert r["new"]                               # some placeholder
    assert r["source"] == "default"


def test_resolve_value_never_invents_concrete_endpoint():
    # no override, no useful match → placeholder form only, never a real host
    r = resolve_value("ip", "10.0.5.99", matches=None)
    assert r["new"].startswith("${")              # a placeholder, not 10.x
    assert r["confidence"] < 1


# ---------------------------------------------------------------------------
# question_for_change — unresolved change → operator question
# ---------------------------------------------------------------------------
def test_question_roundtrip():
    q = Question(app_id="orders", kind="value", prompt="what now?",
                 options=["${DB_HOST}"], context={"file": "a", "line": 1})
    d = q.to_dict()
    q2 = Question.from_dict(d)
    assert q2.app_id == "orders"
    assert q2.options == ["${DB_HOST}"]
    assert q2.context["file"] == "a"
    assert q2.status == "pending"


def test_question_for_change_resolved_item_returns_none():
    change = {"category": "db_connection", "kind": "endpoint", "file": "a",
              "line": 1, "old": "10.0.4.20:3306", "new": "${DB_HOST}:3306"}
    assert question_for_change("orders", change) is None


def test_question_for_change_old_known_new_unknown_suggests_choice():
    change = {"category": "db_connection", "kind": "endpoint", "file": "a",
              "line": 1, "old": "10.0.4.20:3306", "new": "",
              "title": "Parameterize DB host", "evidence": "jdbc:mysql://10.0.4.20"}
    q = question_for_change("orders", change, job_id="cjob-1")
    assert q is not None
    assert q.kind == "choice"
    assert q.options == ["${ENDPOINT}"]          # default placeholder offered
    assert "10.0.4.20:3306" in q.prompt
    assert "a:1" in q.prompt
    assert q.context["old"] == "10.0.4.20:3306"
    assert q.job_id == "cjob-1"


def test_question_for_change_old_unknown_asks_value():
    change = {"category": "secrets_in_repo", "kind": "secret", "file": "a",
              "line": 5, "old": "", "new": ""}
    q = question_for_change("orders", change)
    assert q.kind == "value"
    assert "couldn't extract" in q.prompt
    # secret prompts warn the operator not to paste the literal
    assert "don't paste" not in q.prompt  # old empty → generic "what literal" path


def test_question_for_change_secret_with_old_warns_no_paste():
    change = {"category": "secrets_in_repo", "kind": "secret", "file": "a",
              "line": 5, "old": "hunter2", "new": ""}
    q = question_for_change("orders", change)
    assert "paste" in q.prompt.lower() and "secret" in q.prompt.lower()