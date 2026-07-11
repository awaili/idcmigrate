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
    # data store
    db_url: str = field(default_factory=lambda: _env("IDC_DB_URL", f"sqlite:///{ROOT}/idc_migrate.db"))

    # servicenow
    sn_base: str = field(default_factory=lambda: _env("IDC_SERVICENOW_BASE"))
    sn_user: str = field(default_factory=lambda: _env("IDC_SERVICENOW_USER"))
    sn_password: str = field(default_factory=lambda: _env("IDC_SERVICENOW_PASSWORD"))
    sn_token: str = field(default_factory=lambda: _env("IDC_SERVICENOW_TOKEN"))
    sn_table: str = field(default_factory=lambda: _env("IDC_SERVICENOW_TABLE", "cmdb_ci_server"))
    sn_limit: int = field(default_factory=lambda: _env_int("IDC_SERVICENOW_LIMIT", 10000))

    # rvtools
    rvtools_path: str = field(default_factory=lambda: _env("IDC_RVTOOLS_PATH", str(FIXTURES / "rvtools_vInfo.csv")))

    # zabbix
    zbx_url: str = field(default_factory=lambda: _env("IDC_ZABBIX_URL"))
    zbx_token: str = field(default_factory=lambda: _env("IDC_ZABBIX_TOKEN"))
    zbx_user: str = field(default_factory=lambda: _env("IDC_ZABBIX_USER"))
    zbx_password: str = field(default_factory=lambda: _env("IDC_ZABBIX_PASSWORD"))

    # prometheus
    prom_url: str = field(default_factory=lambda: _env("IDC_PROM_URL"))
    prom_timeout: int = field(default_factory=lambda: _env_int("IDC_PROM_TIMEOUT", 30))

    # llm
    llm_base: str = field(default_factory=lambda: _env("IDC_LLM_BASE", "http://127.0.0.1:11434"))
    llm_model: str = field(default_factory=lambda: _env("IDC_LLM_MODEL", "glm-5.2:cloud"))
    llm_timeout: int = field(default_factory=lambda: _env_int("IDC_LLM_TIMEOUT", 120))
    llm_enabled: bool = field(default_factory=lambda: _env_bool("IDC_LLM_ENABLED", True))

    # claude agent
    claude_bin: str = field(default_factory=lambda: _env("IDC_CLAUDE_BIN", "claude"))
    claude_timeout: int = field(default_factory=lambda: _env_int("IDC_CLAUDE_TIMEOUT", 600))
    claude_default_mode: str = field(default_factory=lambda: _env("IDC_CLAUDE_DEFAULT_MODE", "plan"))

    # derived helpers
    @property
    def sqlite_path(self) -> Path:
        url = self.db_url
        if url.startswith("sqlite:///"):
            p = url[len("sqlite:///"):]
            return Path(p) if p else ROOT / "idc_migrate.db"
        return ROOT / "idc_migrate.db"

    def has_servicenow(self) -> bool:
        return bool(self.sn_base and (self.sn_token or (self.sn_user and self.sn_password)))

    def has_zabbix(self) -> bool:
        return bool(self.zbx_url and (self.zbx_token or (self.zbx_user and self.zbx_password)))

    def has_prometheus(self) -> bool:
        return bool(self.prom_url)


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