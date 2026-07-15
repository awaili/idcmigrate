"""No-dummy-data guard: when IDC_ALLOW_FIXTURE_FALLBACK=false, ingest adapters
with no live creds return empty (NOT the bundled fixture), and load_workloads
honors IDC_APPS_PATH. Protects a live estate with real imported data from being
overwritten by a stray Ingest / Rebuild pulling fixtures."""
import os

from idc.config import Settings
from idc.core.ingest import get_adapter
from idc.core.ingest.base import IngestResult
from idc.core.models import SOURCE_PROMETHEUS, SOURCE_RVTOOLS, SOURCE_SERVICENOW, SOURCE_ZABBIX


def _settings(fallback: bool, **kw):
    import dataclasses
    base = Settings()
    return dataclasses.replace(base, allow_fixture_fallback=fallback, **kw)


def test_fixture_fallback_disabled_returns_empty_not_fixture():
    """With fallback off + no live creds, every adapter returns empty (mode
    'disabled'), not the bundled fixture rows."""
    s = _settings(False)
    for src in (SOURCE_SERVICENOW, SOURCE_ZABBIX, SOURCE_PROMETHEUS):
        res = get_adapter(src).fetch(s)
        assert res.assets == [], f"{src}: should return no assets with fallback off"
        assert res.mode == "disabled"
        assert "fixture fallback is off" in res.error


def test_rvtools_fixture_blocked_when_path_under_fixtures():
    """RVTools pointing at the bundled fixture is blocked when fallback is off."""
    s = _settings(False)
    assert s.rvtools_path  # default points at a bundled fixture
    res = get_adapter(SOURCE_RVTOOLS).fetch(s)
    assert res.assets == []
    assert res.mode == "disabled"


def test_rvtools_uploaded_file_still_honored_when_fallback_off():
    """A real operator-uploaded RVTools file (outside fixtures) is honored even
    with fallback off — the guard only blocks the BUNDLED fixture."""
    import tempfile, dataclasses
    from idc.config import FIXTURES
    row = ("VM,Power On,host-real,4,8192,centos 7,dc1\n")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("VM,PowerState,Name,CPUs,Memory MB,OS,Datacenter\n")
        f.write(row)
        path = f.name
    try:
        base = Settings()
        s = dataclasses.replace(base, allow_fixture_fallback=False, rvtools_path=path)
        res = get_adapter(SOURCE_RVTOOLS).fetch(s)
        assert res.assets, "uploaded RVTools file must be honored with fallback off"
        assert res.mode in ("fixture", "online")   # not 'disabled'
    finally:
        os.unlink(path)


def test_fixture_fallback_on_default_still_loads_fixtures():
    """Default (fallback on) preserves the offline demo: fixtures still load."""
    s = _settings(True)
    for src in (SOURCE_SERVICENOW, SOURCE_ZABBIX, SOURCE_PROMETHEUS):
        res = get_adapter(src).fetch(s)
        assert res.assets, f"{src}: fixtures should load with fallback on (default)"
        assert res.mode == "fixture"


def test_load_workloads_honors_idc_apps_path(monkeypatch, tmp_path):
    """load_workloads reads IDC_APPS_PATH (the imported apps file), not the
    bundled fixture, so rebuild reproduces the imported app grouping."""
    from idc.core.normalize import load_workloads
    apps = tmp_path / "myapps.csv"
    apps.write_text("app_id,name,tier,env,server_hostnames,depends_on,notes\n"
                    "APP_X,APP_X,,,HOST_1;HOST_2,,\n", encoding="utf-8")
    monkeypatch.setenv("IDC_APPS_PATH", str(apps))
    wls = load_workloads()
    assert len(wls) == 1
    assert wls[0].app_id == "APP_X"
    assert wls[0].server_ids == ["HOST_1", "HOST_2"]   # pre-resolve hostnames


def test_load_workloads_falls_back_to_fixture_without_env(monkeypatch):
    monkeypatch.delenv("IDC_APPS_PATH", raising=False)
    from idc.core.normalize import load_workloads
    wls = load_workloads()
    # the bundled fixture's apps.csv has entries (demo) — just assert no crash
    # and that it's a list (the default path).
    assert isinstance(wls, list)