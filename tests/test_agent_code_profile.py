"""Tests for path A — agent (codex) grounded code findings flowing from the
executor's optional codex pass into the CodeProfile + the MigraQ prompts that
consume it (audit_match / seven_r_strategy / review_plan).

Covers: CodeProfile round-trip of agent_* fields, backward-compat (old executor
payloads without agent_*), _code_profile_brief provenance surfacing, and the
two context builders (_seven_r_context / _review_context) feeding agent
blockers into the LLM prompts. No DB, no network.
"""
import json

from idc.core.models import (CodeProfile, ScanFinding, Server, Utilization,
                             Workload, Wave)
from idc.core.match import match_server
from idc.llm.client import (_code_profile_brief, _seven_r_context,
                            _review_context)


def _profile(app_id="APP_1", with_agent=True):
    p = CodeProfile(app_id=app_id, language="python", runtime="python3.11",
                    framework="django", cloud_readiness=0.4,
                    migration_pattern="replatform", refactor_effort="high",
                    blockers=["on-prem mysql"], summary="rule scan")
    if with_agent:
        p.agent_findings = [ScanFinding(
            category="agent_insight", severity="blocker", file="app/models.py",
            line=42, message="Oracle PL/SQL package, no MySQL equiv",
            evidence="EXEC pkg.do_thing();", remediation="rewrite")]
        p.agent_blockers = ["PL/SQL hard blocker"]
        p.agent_summary = "Needs refactor before replatform."
    return p


def _extract_payload(ctx):
    """Pull the first balanced {...} JSON object out of a context string
    (the prompt wraps it in prose: '... (JSON):\\n{...}\\n\\nOutput ...')."""
    start = ctx.index("{")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(ctx)):
        ch = ctx[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(ctx[start:i + 1])
    raise AssertionError("no balanced JSON object found")


# ---------------------------------------------------------------------------
# CodeProfile round-trip + backward-compat
# ---------------------------------------------------------------------------
def test_code_profile_round_trip_agent_fields():
    p = _profile()
    d = p.to_dict()
    # to_dict carries the agent fields
    assert "agent_findings" in d and "agent_blockers" in d and "agent_summary" in d
    p2 = CodeProfile.from_dict(d)
    # from_dict parses the dict-shaped agent_findings into ScanFinding objects
    assert isinstance(p2.agent_findings[0], ScanFinding)
    assert p2.agent_findings[0].category == "agent_insight"
    assert p2.agent_findings[0].severity == "blocker"
    assert p2.agent_blockers == ["PL/SQL hard blocker"]
    assert p2.agent_summary == "Needs refactor before replatform."


def test_code_profile_from_dict_back_compat_no_agent_fields():
    """An OLD executor payload (pre-codex-pass) has no agent_* keys — from_dict
    must still parse with empty agent fields, not raise."""
    old = {"app_id": "APP_1", "language": "go", "runtime": "go1.21",
           "cloud_readiness": 0.9, "migration_pattern": "rehost",
           "findings": [{"category": "hardcoded_ip", "severity": "medium",
                         "file": "main.go", "line": 9, "message": "ip"}]}
    p = CodeProfile.from_dict(old)
    assert p.agent_findings == []
    assert p.agent_blockers == []
    assert p.agent_summary == ""
    # the rule findings still parse
    assert len(p.findings) == 1 and p.findings[0].category == "hardcoded_ip"


def test_code_profile_from_dict_agent_findings_already_deserialized():
    """Idempotent: a row may already carry ScanFinding objects (the store
    pre-deserializes) — from_dict keeps those as-is, like it does for findings."""
    sf = ScanFinding(category="agent_insight", severity="blocker",
                    file="x.py", line=1, message="m")
    p = CodeProfile.from_dict({"app_id": "A", "agent_findings": [sf]})
    assert p.agent_findings[0] is sf  # not re-parsed


# ---------------------------------------------------------------------------
# _code_profile_brief — provenance surfacing into the audit/right-size prompts
# ---------------------------------------------------------------------------
def test_code_profile_brief_surfaces_agent_blockers():
    b = _code_profile_brief(_profile())
    assert b["agent_blockers"] == ["PL/SQL hard blocker"]
    assert b["agent_findings_count"] == 1
    assert b["agent_summary"] == "Needs refactor before replatform."
    # the rule blockers still there
    assert b["blockers"] == ["on-prem mysql"]


def test_code_profile_brief_empty_agent_fields_when_absent():
    # a rule-only profile (no codex pass) → empty agent fields, not missing keys
    b = _code_profile_brief(_profile(with_agent=False))
    assert b["agent_blockers"] == []
    assert b["agent_findings_count"] == 0
    assert b["agent_summary"] == ""


def test_code_profile_brief_none_when_no_profile():
    assert _code_profile_brief(None) is None


# ---------------------------------------------------------------------------
# _seven_r_context — agent blockers fed into the 7R strategist's code dict
# ---------------------------------------------------------------------------
def _mk_server(app_id="APP_1"):
    return Server(hostname="app-01", role="app", os="centos", cpu_cores=4,
                   mem_gb=8, app_ids=[app_id],
                   utilization=Utilization(cpu_p95=40.0, mem_p95=50.0))


def test_seven_r_context_includes_agent_blockers():
    s = _mk_server()
    w = Workload(app_id="APP_1", name="app one", server_ids=[s.id])
    m = match_server(s)
    ctx = _seven_r_context("APP_1", [s], [w], [m], [_profile()])
    # the code block carries both the rule blockers + the agent blockers
    payload = _extract_payload(ctx)
    code = payload["code_profile"]
    assert code["agent_blockers"] == ["PL/SQL hard blocker"]
    assert code["agent_findings_count"] == 1
    assert "refactor" in code["agent_summary"]
    # rule blockers still present
    assert code["blockers"] == ["on-prem mysql"]


def test_seven_r_context_no_agent_fields_when_rule_only():
    s = _mk_server()
    w = Workload(app_id="APP_1", name="app one", server_ids=[s.id])
    m = match_server(s)
    ctx = _seven_r_context("APP_1", [s], [w], [m], [_profile(with_agent=False)])
    payload = _extract_payload(ctx)
    code = payload["code_profile"]
    assert code["agent_blockers"] == []
    assert code["agent_findings_count"] == 0
    assert code["agent_summary"] == ""


# ---------------------------------------------------------------------------
# _review_context — per-app agent blockers fed into the plan red-team
# ---------------------------------------------------------------------------
def test_review_context_includes_app_agent_blockers():
    s = _mk_server()
    wv = Wave(id="w1", name="Apps", stage="3_application", server_ids=[s.id])
    wl = Workload(app_id="APP_1", name="app one", server_ids=[s.id])
    m = match_server(s)
    ctx = _review_context([wv], [s], [wl], [m], [_profile()], strategies=None)
    payload = _extract_payload(ctx)
    # the agent blocker for APP_1 surfaces so the red-team can flag it
    assert payload["app_agent_blockers"] == {"APP_1": ["PL/SQL hard blocker"]}


def test_review_context_no_app_agent_blockers_when_rule_only():
    s = _mk_server()
    wv = Wave(id="w1", name="Apps", stage="3_application", server_ids=[s.id])
    wl = Workload(app_id="APP_1", name="app one", server_ids=[s.id])
    m = match_server(s)
    ctx = _review_context([wv], [s], [wl], [m], [_profile(with_agent=False)],
                           strategies=None)
    payload = _extract_payload(ctx)
    assert payload["app_agent_blockers"] == {}