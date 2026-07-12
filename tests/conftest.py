"""Pytest configuration: run the DB-backed suite against a dedicated test
database, never the live ``idc_migrate`` DB that ``idc-migrate.service`` serves.

Why this exists: until now every DB-backed test opened ``open_store(get_settings()
.db_url)`` and wrote to the SAME ``idc_migrate`` database the live backend on
:8010 uses — isolation was only random IDs + best-effort manual ``DELETE`` (leaks
on a crash/assert), and a ``rebuild`` test would ``DELETE FROM servers`` on the
real 15K estate. This conftest points the suite at ``idc_migrate_test`` instead.

How: the redirect happens in ``pytest_configure`` — BEFORE test modules are
imported. ``idc.backend.app`` opens its module-level ``STORE =
open_store(settings.db_url)`` at import time, so the env override must land
first or that ``STORE`` pins to the live DB for the whole session.

Override: set ``IDC_TEST_DB_URL`` to point the suite at a different test DB.
A ``IDC_DB_URL`` that targets a non-live database is also respected. The live
``idc_migrate`` database is refused outright (see ``pytest_configure``) so no
test run can mutate production data.

One-time DB-box setup (run once as root on the DB box; not in the repo)::

    CREATE DATABASE IF NOT EXISTS idc_migrate_test CHARACTER SET utf8mb4;
    GRANT ALL PRIVILEGES ON idc_migrate_test.* TO 'idc_migrate_app'@'10.0.0.%';
    FLUSH PRIVILEGES;

``open_store`` auto-creates the schema on the test DB, so no further bootstrap
is needed.
"""
import os
from urllib.parse import urlparse

LIVE_DB = "idc_migrate"
TEST_DB = "idc_migrate_test"


def _target_url() -> str:
    """Resolve the DB URL the suite should run against (never the live DB)."""
    if os.environ.get("IDC_TEST_DB_URL"):
        return os.environ["IDC_TEST_DB_URL"]
    # Derive from the app's configured DB (same host/creds) but swap the db
    # name, so the password isn't duplicated here. Settings() reads env without
    # touching the get_settings() singleton, so this can't pin the live URL.
    from idc.config import Settings
    base = Settings().db_url          # honors IDC_DB_URL if set; else live default
    dbname = (urlparse(base).path or "").lstrip("/")
    if dbname and dbname != LIVE_DB:
        return base                   # operator pointed IDC_DB_URL at a non-live DB
    p = urlparse(base)
    return f"{p.scheme}://{p.netloc}/{TEST_DB}"


def pytest_configure(config):
    url = _target_url()
    dbname = (urlparse(url).path or "").lstrip("/")
    if dbname == LIVE_DB:
        raise Exception(
            f"Refusing to run tests against the live database '{LIVE_DB}'. "
            f"Create the test DB '{TEST_DB}' on the DB box and grant the app "
            f"user ALL on it, or set IDC_TEST_DB_URL to a non-live database.")
    os.environ["IDC_DB_URL"] = url
    # Make get_settings() re-read the env (the singleton may already be cached).
    from idc.config import reset_settings
    reset_settings()


import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _clean_test_db_slate():
    """Give each pytest invocation a clean slate on the test DB.

    Truncates every table at session start. Tests seed their own rows and use
    random IDs + their own cleanup, so a fresh start is safe and makes runs
    deterministic regardless of leftover rows from a previously crashed run.
    Failures here (e.g. DB unreachable) are logged, not fatal — pure tests
    still run, and DB-backed tests surface the real error individually.
    """
    try:
        from idc.config import get_settings
        from idc.core.db import open_store
        st = open_store(get_settings().db_url)
        conn = st._pool_get()
        try:
            cur = conn.cursor()
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("SHOW TABLES")
            for row in cur.fetchall():
                table = next(iter(row.values()))
                cur.execute(f"TRUNCATE TABLE `{table}`")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        finally:
            st._pool_put(conn)
        st.close()
    except Exception as e:  # pragma: no cover - best-effort hygiene
        print(f"\n[conftest] skipping test-DB clean slate: {e}")
    yield