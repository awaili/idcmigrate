"""F4 — data-coverage confidence rating tests.

Pure-function tests for sizing_basis_of / assessment_confidence_of /
enrich_match(assessment_confidence=...), plus one DB-backed rebuild
integration test confirming the signals are persisted and the final match
confidence is scaled by coverage.
"""
import pytest

from idc.core.match import (
    assessment_confidence_of, match_server, sizing_basis_of,
)
from idc.core.models import (
    CodeProfile, Match, Server, Target, Utilization,
    PATTERN_REFACTOR,
)
from idc.core.codeintel import enrich_match


def _srv(**kw) -> Server:
    s = Server(hostname=kw.pop("hostname", "h"), role=kw.pop("role", "app"),
               os=kw.pop("os", "centos"), cpu_cores=kw.pop("cpu_cores", 4),
               mem_gb=kw.pop("mem_gb", 8), env=kw.pop("env", "prod"),
               business_criticality=kw.pop("business_criticality", "medium"))
    s.source_refs = kw.pop("source_refs", [])
    s.utilization = kw.pop("utilization", Utilization())
    s.app_ids = kw.pop("app_ids", [])
    return s


# -- sizing_basis_of -------------------------------------------------------
def test_sizing_basis_measured_when_live_util_complete():
    s = _srv(utilization=Utilization(cpu_p95=42, mem_p95=60, source="prometheus"))
    assert sizing_basis_of(s) == "measured"
    s2 = _srv(utilization=Utilization(cpu_p95=42, mem_p95=60, source="zabbix"))
    assert sizing_basis_of(s2) == "measured"


def test_sizing_basis_estimated_when_util_missing_or_partial():
    assert sizing_basis_of(_srv(utilization=Utilization())) == "estimated"
    # cpu present but no mem -> not enough to size off utilization
    s = _srv(utilization=Utilization(cpu_p95=42, mem_p95=None, source="prometheus"))
    assert sizing_basis_of(s) == "estimated"
    # utilization present but from a non-telemetry source
    s2 = _srv(utilization=Utilization(cpu_p95=42, mem_p95=60, source=""))
    assert sizing_basis_of(s2) == "estimated"


# -- assessment_confidence_of ---------------------------------------------
def test_assessment_confidence_full_coverage_is_one():
    s = _srv(source_refs=[
        {"source": "servicenow"}, {"source": "rvtools"},
        {"source": "zabbix"}, {"source": "prometheus"},
    ], utilization=Utilization(cpu_p95=50, mem_p95=60, source="zabbix"))
    # 0.25*1 + 0.35*1 + 0.20*1 + 0.20*1 = 1.0 (all key fields set, has profile)
    assert assessment_confidence_of(s, has_code_profile=True) == 1.0


def test_assessment_confidence_cmdb_only_is_low():
    # only servicenow, no utilization, no code profile
    s = _srv(source_refs=[{"source": "servicenow"}])
    score = assessment_confidence_of(s, has_code_profile=False)
    # 0.25*0.25 + 0.35*0 + 0.20*1 (all key fields set) + 0.20*0 = 0.2625
    assert 0.2 <= score <= 0.35
    assert 0.0 <= score <= 1.0


def test_assessment_confidence_clamped_and_monotonic_with_profile():
    s = _srv(source_refs=[{"source": "servicenow"}, {"source": "zabbix"}],
             utilization=Utilization(cpu_p95=50, mem_p95=60, source="zabbix"))
    base = assessment_confidence_of(s, has_code_profile=False)
    with_profile = assessment_confidence_of(s, has_code_profile=True)
    assert with_profile > base  # adding a profile raises the score
    assert with_profile <= 1.0


# -- enrich_match with coverage -------------------------------------------
def test_enrich_match_coverage_scales_confidence_and_notes():
    m = Match(server_id="srv-1", target=Target(product="CVM"), confidence=0.9,
              rationale="app server -> CVM")
    out = enrich_match(m, profile=None, assessment_confidence=0.5)
    assert out.confidence == pytest.approx(0.45, abs=0.001)
    assert "data-coverage 0.50" in out.rationale


def test_enrich_match_coverage_floors_at_0_05():
    m = Match(server_id="srv-1", confidence=0.08, rationale="x")
    out = enrich_match(m, profile=None, assessment_confidence=0.1)
    assert out.confidence == pytest.approx(0.05, abs=0.001)


def test_enrich_match_default_coverage_is_identity_no_note():
    # default assessment_confidence=1.0 + no profile -> no change, no note
    m = Match(server_id="srv-1", confidence=0.9, rationale="base")
    out = enrich_match(m, profile=None)
    assert out.confidence == 0.9
    assert out.rationale == "base"


def test_enrich_match_coverage_composes_with_blocker_dip():
    prof = CodeProfile(app_id="a", migration_pattern=PATTERN_REFACTOR,
                       blockers=["b1"], cloud_readiness=0.9)
    m = Match(server_id="srv-1", confidence=0.9, rationale="base")
    # blocker dip: max(0.05, 0.9 - 0.12) = 0.78; then x0.5 coverage = 0.39
    out = enrich_match(m, profile=prof, assessment_confidence=0.5)
    assert out.confidence == pytest.approx(0.39, abs=0.01)


# -- rebuild integration (DB-backed) --------------------------------------
pytest.importorskip("idc.backend.app")  # opens the DB pool; skip if DB down
from idc.config import get_settings
from idc.core.db import open_store
from idc.core import rebuild, run_ingest


def test_rebuild_persists_coverage_and_scales_match_confidence():
    """Ingest the bundled fixtures + rebuild, then verify persisted servers
    carry sizing_basis + assessment_confidence and matches are scaled by
    coverage (confidence <= the base rule confidence for the same role)."""
    st = open_store(get_settings().db_url)
    try:
        run_ingest(st, get_settings(), source="all")
        rebuild(st, do_match=True, do_plan=True)
        servers = st.list_all_servers()
        assert servers, "fixtures should produce servers"
        # every persisted server carries the F4 signals
        for s in servers:
            assert s.sizing_basis in ("measured", "estimated")
            assert s.assessment_confidence is not None
            assert 0.0 <= s.assessment_confidence <= 1.0
        # at least one measured host (fixtures include zabbix/prom utilization)
        measured = [s for s in servers if s.sizing_basis == "measured"]
        assert measured, "fixtures should include at least one host with live util"
        # matches: a host with coverage<1 must have confidence <= its base rule
        # confidence (base is 0.7-0.9 by role); i.e. coverage scaled it down.
        matches = {m.server_id: m for m in st.list_matches()}
        low_coverage = [s for s in servers
                        if s.assessment_confidence is not None and s.assessment_confidence < 0.99]
        assert low_coverage, "fixture servers (no code profiles) should have coverage<1"
        for s in low_coverage:
            if s.id in matches:
                m = matches[s.id]
                # base rule confidence is at least 0.7 for all roles; coverage<1
                # means the persisted confidence should be strictly below 0.9.
                assert m.confidence < 0.9, (s.hostname, m.confidence, s.assessment_confidence)
    finally:
        st.close()