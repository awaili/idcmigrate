"""F3 — business-criticality ordering + downtime-window field tests."""
import pytest

from idc.core.codeintel import (
    CRIT_HIGH, CRIT_LOW, CRIT_MEDIUM, app_criticality_rank,
)
from idc.core.models import Server, Wave, Workload
from idc.core.plan import plan_waves


def _S(sid, host, role, apps, crit=""):
    return Server(id=sid, hostname=host, role=role, app_ids=apps,
                  business_criticality=crit)


def test_app_criticality_rank_high_wins_over_low():
    s_hi = _S("s1", "h", "app", ["a"], "high")
    s_lo = _S("s2", "h2", "app", ["a"], "low")
    assert app_criticality_rank("a", [s_hi, s_lo], []) == 0   # high = rank 0


def test_app_criticality_rank_unknown_defaults_medium():
    s = _S("s", "h", "app", ["a"])           # no criticality declared
    assert app_criticality_rank("a", [s], []) == 1            # medium
    assert app_criticality_rank("ghost", [], []) == 1         # no servers


def test_app_criticality_rank_low_is_two():
    s = _S("s", "h", "app", ["a"], "low")
    assert app_criticality_rank("a", [s], []) == 2


def test_plan_waves_orders_high_crit_app_before_low_crit_in_same_level():
    """Two independent apps (no inter-dep) at the same topo level: the high-crit
    one should be sequenced into an earlier wave than the low-crit one."""
    hi = _S("sh", "web-hi", "app", ["app-hi"], "high")
    lo = _S("sl", "web-lo", "app", ["app-lo"], "low")
    wls = [
        Workload(app_id="app-hi", name="Hi", server_ids=["sh"], depends_on=[]),
        Workload(app_id="app-lo", name="Lo", server_ids=["sl"], depends_on=[]),
    ]
    waves = plan_waves([hi, lo], wls)
    # find the two app waves (skip LZ/Data which are empty here)
    app_waves = [w for w in waves if "app-hi" in (w.name + w.rationale) or
                 "app-lo" in (w.name + w.rationale)]
    # the high-crit app's wave must come first
    names = [w.name for w in app_waves]
    hi_idx = next(i for i, n in enumerate(names) if "Hi" in n)
    lo_idx = next(i for i, n in enumerate(names) if "Lo" in n)
    assert hi_idx < lo_idx


def test_wave_has_downtime_window_field_default_empty():
    w = Wave(name="W1")
    assert w.downtime_window == {}
    # roundtrip preserves an operator-set window
    w.downtime_window = {"start": "2026-07-20T02:00Z", "end": "2026-07-20T04:00Z"}
    d = w.to_dict()
    assert d["downtime_window"]["start"] == "2026-07-20T02:00Z"
    w2 = Wave.from_dict(d)
    assert w2.downtime_window["end"] == "2026-07-20T04:00Z"