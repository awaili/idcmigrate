"""Data-gap — OS/runtime end-of-support detection tests (the silent-rehost fix).

Covers ``eol.os_eol_bucket`` / ``runtime_eol_bucket`` (the EOL table lookup +
expired/expiring/active/unknown classification), ``eol.apply_os_eol`` (the match
confidence dip + replatform note/alternative that stops a blind rehost of
CentOS 6 / Windows 2008), and the readiness ``os_support`` signal.
"""
import pytest

from idc.core.eol import (
    os_eol_bucket, os_eol_date, runtime_eol_bucket, apply_os_eol,
    EXPIRED, EXPIRING, ACTIVE, UNKNOWN,
)
from idc.core.match import match_server
from idc.core.models import Server, Match, Target, Wave, _new_id, STAGE_APP
from idc.core.readiness import _sig_os_support, GREEN, YELLOW, RED, NA

TODAY = "2026-07-12"


def _srv(os="", os_version=""):
    return Server(id=_new_id("srv"), hostname="h", os=os, os_version=os_version,
                  role="web", cpu_cores=2, mem_gb=4)


# -- os_eol_bucket --------------------------------------------------------
def test_os_eol_centos7_expired_centos6_expired():
    assert os_eol_bucket(_srv("centos", "7"), TODAY) == EXPIRED   # EOL 2024-06
    assert os_eol_bucket(_srv("centos", "6"), TODAY) == EXPIRED   # EOL 2020-11


def test_os_eol_windows_2008_2012_expired():
    assert os_eol_bucket(_srv("windows", "2008"), TODAY) == EXPIRED
    assert os_eol_bucket(_srv("windows server", "2012 r2"), TODAY) == EXPIRED


def test_os_eol_active_and_expiring():
    # Windows 2019 EOL 2029-01 -> active
    assert os_eol_bucket(_srv("windows", "2019"), TODAY) == ACTIVE
    # Ubuntu 20.04 EOL 2025-04 -> expired (before 2026-07); Debian 11 EOL
    # 2026-08-15 -> expiring (within 90d of 2026-07-12)
    assert os_eol_bucket(_srv("ubuntu", "20.04"), TODAY) == EXPIRED
    assert os_eol_bucket(_srv("debian", "11"), TODAY) == EXPIRING


def test_os_eol_unknown_for_unmatched():
    assert os_eol_bucket(_srv("aix", "7"), TODAY) == UNKNOWN
    assert os_eol_bucket(_srv("centos", ""), TODAY) == UNKNOWN    # no version
    assert os_eol_bucket(_srv("", "7"), TODAY) == UNKNOWN          # no os
    assert os_eol_date(_srv("freebsd", "13")) == ""


# -- runtime_eol_bucket ---------------------------------------------------
def test_runtime_eol_jdk_and_python():
    assert runtime_eol_bucket("jdk7", today_iso=TODAY) == EXPIRED
    assert runtime_eol_bucket("jdk8", today_iso=TODAY) == ACTIVE
    # jdk11 EOL 2026-09-30 -> within 90d of 2026-07-12 -> expiring
    assert runtime_eol_bucket("openjdk-11", today_iso=TODAY) == EXPIRING
    assert runtime_eol_bucket("python2", today_iso=TODAY) == EXPIRED
    assert runtime_eol_bucket("go1.20", today_iso=TODAY) == UNKNOWN   # not in table


# -- apply_os_eol (the silent-rehost fix) ----------------------------------
def test_apply_os_eol_dips_confidence_and_notes_for_expired():
    s = _srv("centos", "7")
    m = match_server(s)  # CVM rehost, confidence 0.85
    base_conf = m.confidence
    apply_os_eol(m, s, today_iso=TODAY)
    assert m.confidence < base_conf                       # dipped
    assert m.confidence >= 0.05
    assert "OS expired" in m.rationale
    assert "replatform" in (m.alternatives[-1].lower())   # replatform alt added


def test_apply_os_eol_expiring_smaller_dip():
    s = _srv("debian", "11")  # expiring
    m = match_server(s)
    base_conf = m.confidence
    apply_os_eol(m, s, today_iso=TODAY)
    assert m.confidence < base_conf
    assert "OS expiring" in m.rationale


def test_apply_os_eol_noop_for_active_or_unknown():
    for os_, ver in [("windows", "2019"), ("aix", "7"), ("centos", "")]:
        s = _srv(os_, ver)
        m = match_server(s)
        before = (m.confidence, m.rationale, list(m.alternatives))
        apply_os_eol(m, s, today_iso=TODAY)
        assert (m.confidence, m.rationale, list(m.alternatives)) == before


# -- readiness os_support signal ------------------------------------------
def test_os_support_signal_levels():
    we = Wave(id=_new_id("wave"), server_ids=["a"], stage=STAGE_APP)
    wx = Wave(id=_new_id("wave"), server_ids=["b"], stage=STAGE_APP)
    wa = Wave(id=_new_id("wave"), server_ids=["c"], stage=STAGE_APP)
    wu = Wave(id=_new_id("wave"), server_ids=["d"], stage=STAGE_APP)
    servers = [
        Server(id="a", hostname="a", os="centos", os_version="7"),       # expired
        Server(id="b", hostname="b", os="debian", os_version="11"),      # expiring
        Server(id="c", hostname="c", os="windows", os_version="2019"),   # active
        Server(id="d", hostname="d", os="aix", os_version="7"),          # unknown
    ]
    assert _sig_os_support(we, servers)["level"] == RED
    assert _sig_os_support(wx, servers)["level"] == YELLOW
    assert _sig_os_support(wa, servers)["level"] == GREEN
    assert _sig_os_support(wu, servers)["level"] == NA