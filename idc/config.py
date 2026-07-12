"""Env-driven configuration for idc-migrate.

Every setting has a default that lets the system run offline against the
bundled fixtures in ``fixtures/``. Real API credentials are picked up from
the environment (or a ``.env`` file loaded by the CLI/backend entrypoints).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


ROOT = Path(__file__).resolve().parent.parent
# IDC_FIXTURES_DIR lets the deployed instance point at a larger fixture set
# (e.g. fixtures_scale/ for a 15K demo) while tests keep using fixtures/.
FIXTURES = Path(os.environ.get("IDC_FIXTURES_DIR") or (ROOT / "fixtures"))


@dataclass
class Settings:
    # data store — MariaDB on the DB box (10.0.0.3:3306). URL-encode special
    # chars in the password (e.g. '#' -> %23, '!' -> %21).
    db_url: str = field(default_factory=lambda: _env(
        "IDC_DB_URL", "mysql://idc_migrate_app:IdcMigrate%232026%21@10.0.0.3:3306/idc_migrate"))

    # servicenow
    sn_base: str = field(default_factory=lambda: _env("IDC_SERVICENOW_BASE"))
    sn_user: str = field(default_factory=lambda: _env("IDC_SERVICENOW_USER"))
    sn_password: str = field(default_factory=lambda: _env("IDC_SERVICENOW_PASSWORD"))
    sn_token: str = field(default_factory=lambda: _env("IDC_SERVICENOW_TOKEN"))
    sn_table: str = field(default_factory=lambda: _env("IDC_SERVICENOW_TABLE", "cmdb_ci_server"))
    sn_limit: int = field(default_factory=lambda: _env_int("IDC_SERVICENOW_LIMIT", 10000))
    # offline fixture / uploaded file path (defaults to bundled fixture)
    sn_path: str = field(default_factory=lambda: _env("IDC_SERVICENOW_PATH", str(FIXTURES / "servicenow_cmdb_ci_server.csv")))

    # rvtools (always file-based — vSphere export, no online API)
    rvtools_path: str = field(default_factory=lambda: _env("IDC_RVTOOLS_PATH", str(FIXTURES / "rvtools_vInfo.csv")))

    # zabbix
    zbx_url: str = field(default_factory=lambda: _env("IDC_ZABBIX_URL"))
    zbx_token: str = field(default_factory=lambda: _env("IDC_ZABBIX_TOKEN"))
    zbx_user: str = field(default_factory=lambda: _env("IDC_ZABBIX_USER"))
    zbx_password: str = field(default_factory=lambda: _env("IDC_ZABBIX_PASSWORD"))
    # offline fixture / uploaded file path (defaults to bundled fixture)
    zbx_path: str = field(default_factory=lambda: _env("IDC_ZABBIX_PATH", str(FIXTURES / "zabbix_hosts.json")))

    # prometheus
    prom_url: str = field(default_factory=lambda: _env("IDC_PROM_URL"))
    prom_timeout: int = field(default_factory=lambda: _env_int("IDC_PROM_TIMEOUT", 30))
    # offline fixture / uploaded file path (defaults to bundled fixture)
    prom_path: str = field(default_factory=lambda: _env("IDC_PROMETHEUS_PATH", str(FIXTURES / "prometheus_metrics.json")))

    # llm
    llm_base: str = field(default_factory=lambda: _env("IDC_LLM_BASE", "http://127.0.0.1:11434"))
    llm_model: str = field(default_factory=lambda: _env("IDC_LLM_MODEL", "glm-5.2:cloud"))
    llm_timeout: int = field(default_factory=lambda: _env_int("IDC_LLM_TIMEOUT", 120))
    llm_enabled: bool = field(default_factory=lambda: _env_bool("IDC_LLM_ENABLED", True))

    # claude agent
    claude_bin: str = field(default_factory=lambda: _env("IDC_CLAUDE_BIN", "claude"))
    claude_timeout: int = field(default_factory=lambda: _env_int("IDC_CLAUDE_TIMEOUT", 600))
    claude_default_mode: str = field(default_factory=lambda: _env("IDC_CLAUDE_DEFAULT_MODE", "plan"))

    # external agent executor (code scan / comb / modify)
    # IDC_EXECUTOR_URL = base URL of the executor (idc→executor direction).
    # IDC_EXECUTOR_TOKEN = shared bearer secret (also validates push callbacks).
    executor_url: str = field(default_factory=lambda: _env("IDC_EXECUTOR_URL"))
    executor_token: str = field(default_factory=lambda: _env("IDC_EXECUTOR_TOKEN"))
    executor_timeout: int = field(default_factory=lambda: _env_int("IDC_EXECUTOR_TIMEOUT", 600))
    executor_enabled: bool = field(default_factory=lambda: _env_bool("IDC_EXECUTOR_ENABLED", True))

    # F2 — Tencent Cloud pricing source for TCO / business case.
    # IDC_PRICING_URL = public pricing endpoint; empty -> cost.py falls back
    # to a bundled price fixture so the business case still renders out of box.
    # IDC_PRICING_OVERRIDE_PATH = optional JSON {old_sku_or_target: price} map
    # for customer contract pricing (same old->new override shape as executor).
    pricing_url: str = field(default_factory=lambda: _env("IDC_PRICING_URL"))
    pricing_override_path: str = field(default_factory=lambda: _env("IDC_PRICING_OVERRIDE_PATH"))

    # F6.5 — Tencent SMS/DTS migration runner. Empty by default, so F6 runs in
    # "track only" mode (operator / external tool performs the migration,
    # idc-migrate records state + runs validation gates). Wire when SMS API
    # access is available.
    sms_base: str = field(default_factory=lambda: _env("IDC_SMS_BASE"))
    sms_region: str = field(default_factory=lambda: _env("IDC_SMS_REGION"))
    sms_token: str = field(default_factory=lambda: _env("IDC_SMS_TOKEN"))

    # F1 — network dependency discovery source.
    # prometheus (custom connection exporter, falls back to fixture) |
    # zabbix (custom item, falls back to fixture) | collector (agentless
    # TCP-connection snapshot, the realistic primary source) | off (disabled).
    # Standard Prometheus/Zabbix exporters do NOT expose per-connection dst
    # ip:port, so the fixture (an `ss -tn`-style snapshot) is the default
    # out-of-the-box source; live sources fall back to it on error/empty.
    netdep_source: str = field(default_factory=lambda: _env("IDC_NETDEP_SOURCE", "collector"))
    netdep_days: int = field(default_factory=lambda: _env_int("IDC_NETDEP_DAYS", 7))
    # offline/fixture snapshot (default bundled fixture, like the other sources)
    netdep_path: str = field(default_factory=lambda: _env("IDC_NETDEP_PATH", str(FIXTURES / "netdep.json")))

    # derived helpers
    def has_servicenow(self) -> bool:
        return bool(self.sn_base and (self.sn_token or (self.sn_user and self.sn_password)))

    def has_zabbix(self) -> bool:
        return bool(self.zbx_url and (self.zbx_token or (self.zbx_user and self.zbx_password)))

    def has_prometheus(self) -> bool:
        return bool(self.prom_url)

    def has_pricing(self) -> bool:
        """True if a live pricing endpoint is configured (else cost.py uses
        the bundled price fixture so the business case still renders)."""
        return bool(self.pricing_url)

    def has_sms(self) -> bool:
        """True if the Tencent SMS/DTS runner is wired. False -> F6 track-only."""
        return bool(self.sms_base and self.sms_token)

    def netdep_enabled(self) -> bool:
        """True if network-dependency discovery should run during rebuild.

        The fixture snapshot is always available, so any source other than
        ``off`` enables discovery (live sources fall back to the fixture on
        error/empty, like the inventory adapters)."""
        return (self.netdep_source or "").strip().lower() not in ("", "off")


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Force re-read of env on next get_settings() (used by tests)."""
    global _settings
    _settings = None


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env loader (no python-dotenv dependency)."""
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        # strip inline comments only if value is unquoted
        if v and v[0] not in ('"', "'") and "#" in v:
            v = v.split("#", 1)[0].strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v