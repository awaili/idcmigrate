"""Regression tests for the 3rd expert+AI review pass (2026-07-14).

Each test pins one concrete defect fixed in this pass:
  * execute._dominant_status reported a fully-terminal (finalized + rolled-back)
    wave as ``running`` forever.
  * the test_regression / db_conversion gates swallowed store errors into a
    ``pass`` verdict (fail-open on a must_pass cutover gate).
  * llm/client._lz_estate_context used a substring check (``"pci" in tl``) that
    flagged ``specification`` (specifi[pci]ation) as a PCI-regulated estate.
  * llm/planner.propose / _revise called ``LLMClient.chat()`` without a
    try/except, so a gateway error raised a raw 500 instead of degrading.
  * network.build_network_design ignored the policy ``tiers`` override; the
    unplaced check keyed on ``identity_key`` alone so hostname-less servers
    (placed under ``id:<id>``) were falsely reported unplaced.
  * executor_mock picked a Linux TencentOS base image for a Windows host and
    built test-case names that disagreed between gen / run / compare.
"""
import json
from unittest.mock import patch

from idc.config import Settings
from idc.core.execute import _dominant_status, run_validation_gates
from idc.core.lz import default_lz_design
from idc.core.models import (
    MJS_FINALIZED, MJS_PLANNED, MJS_REPLICATING, MJS_ROLLED_BACK,
    MigrationJob, Server,
)
from idc.core.network import build_network_design, default_network_policy
from idc.executor_mock.app import _fake_legacy_disposition, _fake_test_cases


# ---------------------------------------------------------------------------
# execute._dominant_status — mixed terminal wave must be "done", not "running"
# ---------------------------------------------------------------------------
def test_dominant_status_all_finalized_is_done():
    assert _dominant_status({MJS_FINALIZED: 5}) == "done"


def test_dominant_status_all_rolled_back_is_rolled_back():
    assert _dominant_status({MJS_ROLLED_BACK: 5}) == "rolled-back"


def test_dominant_status_mixed_terminal_is_done_not_running():
    # a wave where every server reached a terminal state but a mix of
    # finalized + rolled-back is FINISHED — must not render as "running".
    counts = {MJS_FINALIZED: 5, MJS_ROLLED_BACK: 3}
    assert _dominant_status(counts) == "done"


def test_dominant_status_with_in_flight_is_running():
    counts = {MJS_FINALIZED: 2, MJS_REPLICATING: 3, MJS_PLANNED: 1}
    assert _dominant_status(counts) == "running"


def test_dominant_status_all_planned_is_planned():
    assert _dominant_status({MJS_PLANNED: 4}) == "planned"


# ---------------------------------------------------------------------------
# execute.run_validation_gates — must fail CLOSED on a store error
# ---------------------------------------------------------------------------
def _gate_job(gate_kind: str, **extra) -> MigrationJob:
    g = {"name": f"test-{gate_kind}", "kind": gate_kind, "must_pass": True,
         "result": None, "detail": "", "at": ""}
    g.update(extra)
    return MigrationJob(id="j1", server_id="s1", wave_id="w1", kind="host",
                       status=MJS_PLANNED, validation_gates=[g])


def test_test_regression_gate_fail_closed_on_store_error():
    # store.get_test_diff raises -> the gate must NOT evaluate as "pass".
    class _BoomStore:
        def get_test_diff(self, app_id):
            raise RuntimeError("db down")
    job = _gate_job("test_regression", app_id="app-1")
    ok, gates = run_validation_gates(job, Settings(llm_enabled=False),
                                    store=_BoomStore())
    assert ok is False
    assert gates[0]["result"] == "pending"
    assert "store error" in gates[0]["detail"]


def test_db_conversion_gate_fail_closed_on_store_error():
    class _BoomStore:
        def get_db_profile(self, host):
            raise RuntimeError("db down")
    job = _gate_job("db_conversion_blocked", db_server_id="db-1")
    ok, gates = run_validation_gates(job, Settings(llm_enabled=False),
                                    store=_BoomStore())
    assert ok is False
    assert gates[0]["result"] == "pending"


def test_test_regression_gate_pass_when_diff_clean():
    class _CleanStore:
        def get_test_diff(self, app_id):
            class _D:
                regressions = 0
                diff = [{"case": "x"}]
            return _D()
    job = _gate_job("test_regression", app_id="app-1")
    ok, gates = run_validation_gates(job, Settings(llm_enabled=False),
                                    store=_CleanStore())
    assert ok is True
    assert gates[0]["result"] == "pass"


# ---------------------------------------------------------------------------
# llm/client._lz_estate_context — no substring false-positive regulation tags
# ---------------------------------------------------------------------------
def test_lz_estate_context_no_substring_regulation_false_positive():
    from idc.llm.client import _lz_estate_context
    # "specification" contains the substring "pci" (specifi[pci]ation) — the old
    # substring check falsely flagged it as a PCI-regulated estate.
    s = _lz_estate_context([Server(id="s1", hostname="h1", role="app",
                                   tags=["specification", "pci-dss", "internet"])])
    payload = json.loads(s.split("Estate summary (JSON):\n", 1)[1])
    reg = set(payload["regulation_tag_hints"])
    exp = set(payload["exposure_tag_hints"])
    assert "specification" not in reg
    assert "pci-dss" in reg          # exact variant still detected
    assert "internet" in exp


# ---------------------------------------------------------------------------
# llm/planner — graceful degradation when the gateway raises
# ---------------------------------------------------------------------------
def test_planner_propose_degrades_on_chat_error():
    from idc.llm import get_planner
    from idc.llm.client import LLMClient
    servers = [Server(id="s1", hostname="h1", role="app", os="centos",
                      app_ids=["app-a"])]
    workloads = []
    with patch.object(LLMClient, "chat", side_effect=RuntimeError("gateway down")):
        out = get_planner().propose("migrate everything", servers, workloads,
                                    max_waves=3)
    assert out["policy"] is None
    assert out["errors"]
    assert any("gateway down" in e or "MigraQ error" in e for e in out["errors"])


# ---------------------------------------------------------------------------
# network — tiers override takes effect + hostname-less unplaced fix
# ---------------------------------------------------------------------------
def test_network_tiers_override_moves_role_to_a_different_tier():
    # by default an "app" role lands in the "app" subnet; a tiers override that
    # maps app->data must move it (previously the override was silently ignored).
    lz = default_lz_design()
    pol = {**default_network_policy(),
           "tiers": {"app": "data"}}
    servers = [Server(id="s1", hostname="a1", role="app")]
    nd = build_network_design(lz, servers, policy=pol)
    assert nd.validation["ok"], nd.validation["errors"]
    assert nd.placements["a1"]["subnet"] == "data"
    assert nd.placements["a1"]["tier"] == "data"


def test_network_unplaced_check_uses_id_fallback_for_hostname_less():
    # a server with no hostname/fqdn is placed under "id:<id>"; the validator
    # must NOT report it as unplaced (it used to key on identity_key="" only).
    lz = default_lz_design()
    servers = [Server(id="s-only", role="app")]   # no hostname/fqdn
    nd = build_network_design(lz, servers)
    assert "id:s-only" in nd.placements
    assert nd.placements["id:s-only"]["ip"]        # actually placed
    assert nd.validation["ok"] is True, nd.validation["errors"]
    assert not any("unplaced" in e for e in nd.validation["errors"])


# ---------------------------------------------------------------------------
# executor_mock — Windows base image + stable test-case names
# ---------------------------------------------------------------------------
def test_legacy_disposition_windows_host_gets_windows_base():
    # has_source_repo + app role + windows os -> replatform; the base image must
    # be a Windows image, not a Linux TencentOS one.
    out = _fake_legacy_disposition("s1", {"role": "app", "has_source_repo": True,
                                          "os": "Windows Server 2019", "tags": []},
                                   "sc")
    assert out["disposition"] == "replatform"
    assert "windowsservercore" in out["target_base_image"]
    assert "tencentos" not in out["target_base_image"]


def test_legacy_disposition_linux_host_keeps_linux_base():
    out = _fake_legacy_disposition("s1", {"role": "app", "has_source_repo": True,
                                          "os": "centos", "tags": []}, "sc")
    assert out["disposition"] == "replatform"
    assert "tencentos" in out["target_base_image"]


def test_fake_test_cases_names_match_run_and_compare():
    # gen / run / compare all reference the same integration case name.
    names = {c["name"] for c in _fake_test_cases("orders", {})}
    assert "app_index_200" in names
    assert "health" in names
    assert "regression_orders_total" in names