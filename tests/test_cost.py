"""F2 — TCO / business case + tag-driven retain/retire tests."""
import json
import os
from pathlib import Path

import pytest

from idc.config import get_settings, reset_settings
from idc.core.cost import (
    PriceBook, _lookup, _spec_price_key, estimate_portfolio, estimate_server,
    load_pricebook, strategies_from_tags, what_if, TAG_RETAIN, TAG_RETIRE,
)
from idc.core.models import (
    AppStrategy, CostEstimate, Match, Server, Target,
    PATTERN_RETAIN, PATTERN_RETIRE, PATTERN_REHOST, STRATEGY_SOURCE_OPERATOR,
)


def _srv(hostname, app_ids=None, tags=None, sizing="measured"):
    s = Server(hostname=hostname, fqdn=f"{hostname}.dc1.corp", ips=["10.0.0.1"],
               app_ids=app_ids or [], tags=tags or [], sizing_basis=sizing)
    return s


def _mat(server, product, spec, region="ap-bangkok"):
    return Match(server_id=server.id, target=Target(product=product, spec=spec, region=region),
                 confidence=0.9, method="rule", rationale="x")


# -- price book load + lookup ----------------------------------------------
def test_load_pricebook_fixture_default():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    assert book.source == "fixture"
    assert "SA5.MEDIUM4" in book.by_spec.get("CVM", {})
    # CVM SA5.MEDIUM4 ap-bangkok has a monthly price
    m, y = _lookup(book, "CVM", "SA5.MEDIUM4", "ap-bangkok")
    assert m > 0 and y > 0


def test_spec_price_key_cdb_extracts_sku():
    assert _spec_price_key("CDB", "MySQL 8.0 MYSQL.HighMem - 8GB 高可用版(HA)") == "MYSQL.HighMem - 8GB"
    assert _spec_price_key("CVM", "SA5.MEDIUM4") == "SA5.MEDIUM4"
    assert _spec_price_key("TKE", "托管集群(Managed Control Plane)") == "_default"
    assert _spec_price_key("Unknown", "") == "_default"


def test_lookup_falls_back_to_product_default_then_zero():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    # unknown CVM spec -> by_product.CVM fallback
    m, y = _lookup(book, "CVM", "SA5.NONEXISTENT", "ap-bangkok")
    assert m > 0  # by_product.CVM.ap-bangkok monthly
    # unknown product -> 0
    m2, y2 = _lookup(book, "MadeUpProduct", "x", "ap-bangkok")
    assert m2 == 0.0 and y2 == 0.0


# -- estimate_server + portfolio -------------------------------------------
def test_estimate_server_uses_book_and_basis():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv("app-1", sizing="measured")
    m = _mat(s, "CVM", "SA5.MEDIUM4", "ap-bangkok")
    est = estimate_server(s, m, book)
    assert est.target_product == "CVM" and est.spec == "SA5.MEDIUM4"
    assert est.monthly_usd == 60 and est.yearly_usd == 650
    assert est.basis == "measured" and est.pricing_source == "fixture"


def test_estimate_portfolio_sums_and_rollups():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s1 = _srv("a1", app_ids=["app1"])
    s2 = _srv("a2", app_ids=["app2"])
    s3 = _srv("a3", app_ids=["app3"], sizing="estimated")
    matches = [
        _mat(s1, "CVM", "SA5.MEDIUM4", "ap-bangkok"),    # 650/yr
        _mat(s2, "CDB", "MySQL 8.0 MYSQL.HighMem - 8GB 高可用版(HA)", "ap-bangkok"),  # 1190/yr
        _mat(s3, "CVM", "SA5.SMALL2", "ap-bangkok"),      # 270/yr
    ]
    out = estimate_portfolio([s1, s2, s3], matches, book)
    assert out["cloud_yearly"] == pytest.approx(650 + 1190 + 270, abs=0.5)
    assert out["per_product"]["CVM"] == pytest.approx(650 + 270, abs=0.5)
    assert out["per_product"]["CDB"] == pytest.approx(1190, abs=0.5)
    assert out["per_region"]["ap-bangkok"] == pytest.approx(650 + 1190 + 270, abs=0.5)
    assert out["priced_servers"] == 3 and out["unpriced_servers"] == 0
    # no onprem_rate configured -> savings is None
    assert out["annual_savings"] is None


def test_estimate_portfolio_retain_retire_contribute_zero_cloud_cost():
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s1 = _srv("a1", app_ids=["app1"])
    s2 = _srv("a2", app_ids=["app2"])
    matches = [_mat(s1, "CVM", "SA5.MEDIUM4", "ap-bangkok"),
               _mat(s2, "CVM", "SA5.MEDIUM4", "ap-bangkok")]
    strategies = {"app2": AppStrategy(app_id="app2", strategy=PATTERN_RETIRE,
                                     source=STRATEGY_SOURCE_OPERATOR)}
    out = estimate_portfolio([s1, s2], matches, book, strategies=strategies)
    # only app1 counts; app2 (retire) contributes 0
    assert out["cloud_yearly"] == pytest.approx(650, abs=0.5)
    assert out["per_strategy"]["retire"]["yearly"] == 0.0
    assert out["per_strategy"]["rehost"]["yearly"] == pytest.approx(650, abs=0.5)


# -- what-if ----------------------------------------------------------------
def test_what_if_region_same_region_is_noop():
    # Bangkok is the only target region, so re-region to ap-bangkok is a no-op
    # (the override still applies + recomputes, but the price is unchanged).
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    s = _srv("a1", app_ids=["app1"])
    matches = [_mat(s, "CVM", "SA5.MEDIUM4", "ap-bangkok")]  # 650/yr
    out = what_if([s], matches, book, region="ap-bangkok")
    assert out["applied"]["region"] == "ap-bangkok"
    assert out["baseline"]["cloud_yearly"] == pytest.approx(650, abs=0.5)
    assert out["alternate"]["cloud_yearly"] == pytest.approx(650, abs=0.5)
    assert out["delta"] == pytest.approx(0, abs=0.5)


# -- tag-driven retain/retire -----------------------------------------------
def test_strategies_from_tags_retain_and_retire():
    s_retain = _srv("h1", app_ids=["app1", "app2"], tags=["idc:retain"])
    s_retire = _srv("h2", app_ids=["app3"], tags=["idc:retire"])
    s_plain = _srv("h3", app_ids=["app4"], tags=["env=prod"])
    out = strategies_from_tags([s_retain, s_retire, s_plain])
    assert out["app1"].strategy == PATTERN_RETAIN and out["app1"].source == STRATEGY_SOURCE_OPERATOR
    assert out["app2"].strategy == PATTERN_RETAIN
    assert out["app3"].strategy == PATTERN_RETIRE
    assert "app4" not in out


def test_strategies_from_tags_retain_wins_over_retire():
    s = _srv("h", app_ids=["app1"], tags=["idc:retain", "idc:retire"])
    out = strategies_from_tags([s])
    assert out["app1"].strategy == PATTERN_RETAIN


# -- overrides (contract pricing) ------------------------------------------
def test_overrides_set_contract_source_and_onprem_rate(tmp_path, monkeypatch):
    ov = {"onprem_rate": 100,
          "CVM:SA5.MEDIUM4:ap-bangkok": {"monthly": 50, "yearly": 540}}
    ovpath = tmp_path / "overrides.json"
    ovpath.write_text(json.dumps(ov))
    monkeypatch.setenv("IDC_PRICING_OVERRIDE_PATH", str(ovpath))
    reset_settings()
    book = load_pricebook(get_settings(), refresh=True)
    assert book.source == "contract"
    assert book.onprem_rate == 100.0
    m, y = _lookup(book, "CVM", "SA5.MEDIUM4", "ap-bangkok")
    assert m == 50 and y == 540


# -- rebuild integration (DB-backed) ----------------------------------------
pytest.importorskip("idc.backend.app")
from idc.core.db import open_store
from idc.core import rebuild, run_ingest


def test_rebuild_upserts_costs_and_business_case_renders():
    reset_settings()
    st = open_store(get_settings().db_url)
    try:
        run_ingest(st, get_settings(), source="all")
        rebuild(st, do_match=True, do_plan=True)
        # cost_estimates populated for matched servers
        costs = st.list_costs()
        assert costs, "rebuild should upsert a cost row per matched server"
        # any priced server (fixture has CVM/CDB/Redis/EMR hosts)
        priced = [c for c in costs if c.monthly_usd > 0]
        assert priced, "fixture estate should yield priced matches"
        # business case over persisted servers+matches renders a sane total
        servers = st.list_all_servers()
        matches = st.list_matches()
        book = load_pricebook(get_settings(), refresh=True)
        bc = estimate_portfolio(servers, matches, book)
        assert bc["cloud_yearly"] > 0
        assert bc["priced_servers"] > 0
        assert "per_product" in bc and bc["per_product"]
    finally:
        st.close()