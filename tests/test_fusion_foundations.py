"""Foundation tests for the fusion work (P0a/P0b/P0c).

Runs against the real MariaDB store (like test_executor_endpoints); skipped
if the DB is unreachable. Verifies the three new tables (cost_estimates,
migration_jobs, business_case_snapshots) roundtrip and that stats reports
them. The model-layer roundtrips for CostEstimate/MigrationJob are covered
inline here too so this file is the single foundation gate.
"""
import pytest

pytest.importorskip("idc.backend.app")  # opens the DB pool; skip if DB down
from idc.core.db import open_store
from idc.config import get_settings
from idc.core.models import (
    CostEstimate, MigrationJob,
    MJS_PLANNED, MJS_REPLICATING, MJS_TESTED, MJS_FINALIZED, MJS_ROLLED_BACK,
    MIGRATION_JOB_TRANSITIONS, MIGRATION_JOB_TERMINAL,
)

# unique ids so this test never collides with real data
SID = "pytest-found-srv-1"
MJID = "mjob-pytest-found-1"
BCID = "bc-pytest-found-1"


@pytest.fixture()
def st():
    store = open_store(get_settings().db_url)
    yield store
    # cleanup whatever we wrote
    for sid in (SID,):
        store.delete_cost(sid)
    for mjid in (MJID,):
        with store.tx() as cur:
            cur.execute(store._x("DELETE FROM migration_jobs WHERE id=?"), (mjid,))
    with store.tx() as cur:
        cur.execute(store._x("DELETE FROM business_case_snapshots WHERE id=?"), (BCID,))
    store.close()


# -- model layer (P0a) ------------------------------------------------------
def test_model_roundtrips():
    ce = CostEstimate(server_id=SID, target_product="CVM", spec="SA5.MEDIUM4",
                      region="ap-bangkok", monthly_usd=120.5, yearly_usd=1446.0,
                      basis="measured", pricing_source="public")
    d = ce.to_dict()
    assert d["monthly_usd"] == 120.5
    assert CostEstimate.from_dict(d).yearly_usd == 1446.0

    j = MigrationJob(server_id=SID, app_id="orders", wave_id="wave-1", kind="host")
    assert j.status == MJS_PLANNED
    jd = j.to_dict()
    assert MigrationJob.from_dict(jd).status == MJS_PLANNED


def test_state_machine_transitions_well_formed():
    # every status is a key; terminal states have no outgoing edges
    assert set(MIGRATION_JOB_TRANSITIONS) >= {MJS_PLANNED, MJS_REPLICATING,
                                              MJS_TESTED, MJS_FINALIZED, MJS_ROLLED_BACK}
    for terminal in MIGRATION_JOB_TERMINAL:
        assert MIGRATION_JOB_TRANSITIONS[terminal] == ()
    # revert edges exist at every non-terminal stage
    assert MJS_PLANNED in MIGRATION_JOB_TRANSITIONS[MJS_REPLICATING]
    assert MJS_REPLICATING in MIGRATION_JOB_TRANSITIONS[MJS_TESTED]
    # abort-to-rolled_back from any non-terminal
    for status, nxt in MIGRATION_JOB_TRANSITIONS.items():
        if status not in MIGRATION_JOB_TERMINAL:
            assert MJS_ROLLED_BACK in nxt, status


# -- storage layer (P0b) ----------------------------------------------------
def test_cost_roundtrip(st):
    st.delete_cost(SID)  # idempotent baseline
    assert st.get_cost(SID) is None
    ce = CostEstimate(server_id=SID, target_product="CDB", spec="mysql-8.32",
                      region="ap-bangkok", monthly_usd=88.0, yearly_usd=1056.0,
                      basis="estimated", pricing_source="contract")
    st.upsert_cost(ce)
    got = st.get_cost(SID)
    assert got is not None
    assert got.target_product == "CDB" and got.monthly_usd == 88.0
    assert got.basis == "estimated" and got.pricing_source == "contract"
    # upsert is idempotent overwrite
    ce2 = CostEstimate(server_id=SID, target_product="CDB", spec="mysql-8.64",
                       region="ap-bangkok", monthly_usd=160.0, yearly_usd=1920.0,
                       basis="measured", pricing_source="public")
    st.upsert_cost(ce2)
    assert st.get_cost(SID).spec == "mysql-8.64" and st.get_cost(SID).monthly_usd == 160.0
    st.delete_cost(SID)
    assert st.get_cost(SID) is None


def test_migration_job_roundtrip(st):
    j = MigrationJob(id=MJID, server_id=SID, app_id="orders", wave_id="wave-1",
                    kind="host", status=MJS_PLANNED,
                    stage_history=[{"status": MJS_PLANNED, "at": "t", "by": "pytest"}],
                    validation_gates=[{"name": "ssh", "kind": "connect", "must_pass": True}])
    st.upsert_migration_job(j)
    got = st.get_migration_job(MJID)
    assert got is not None
    assert got.status == MJS_PLANNED and got.kind == "host"
    assert got.stage_history[0]["by"] == "pytest"
    assert got.validation_gates[0]["kind"] == "connect"
    # filter by wave + status
    listed = st.list_migration_jobs(wave_id="wave-1", status=MJS_PLANNED)
    assert any(mj.id == MJID for mj in listed)
    # advance + upsert overwrites, preserving JSON fields
    j.status = MJS_REPLICATING
    j.stage_history.append({"status": MJS_REPLICATING, "at": "t2"})
    st.upsert_migration_job(j)
    assert st.get_migration_job(MJID).status == MJS_REPLICATING
    assert len(st.get_migration_job(MJID).stage_history) == 2


def test_business_case_roundtrip(st):
    payload = {"annual_savings": 123456, "per_strategy": {"rehost": 80000}}
    st.save_business_case(BCID, payload, created_at="2026-07-12T00:00:00Z")
    got = st.get_business_case(BCID)
    assert got is not None
    assert got["id"] == BCID
    assert got["payload"]["annual_savings"] == 123456
    listed = st.list_business_cases(limit=50)
    assert any(b["id"] == BCID for b in listed)


def test_stats_reports_new_tables(st):
    s = st.stats()
    assert "cost_estimates" in s and "migration_jobs" in s
    assert isinstance(s["cost_estimates"], int)
    assert isinstance(s["migration_jobs"], int)