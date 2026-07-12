"""Code intelligence from the external agent executor.

The executor scans an app's source and pushes a ``CodeProfile`` (see
``models.py`` / ``docs/agent-executor.md``). This module turns profiles into
the signals the migration pipeline consumes:

* ``merge_code_deps`` — fold code-discovered app deps into ``Workload.depends_on``
  during reconsolidate (augments the static app graph).
* ``effort_rank`` / ``pattern_rank`` / ``order_apps_for_waves`` — secondary
  ordering keys so high-effort / refactor / rewrite apps land in later waves.
* ``cloud_readiness`` lookup + ``enrich_match`` — lower match confidence and
  surface blockers into rationale when code readiness is low.

All functions are pure (no DB, no network) so they are unit-testable.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import (
    AppStrategy,
    CodeProfile,
    ChangeSpec,
    DBConversionProfile,
    DB_DIFFICULTY_A,
    DB_DIFFICULTY_B,
    DB_DIFFICULTY_C,
    Match,
    MIGRATING_PATTERNS,
    NON_MIGRATING_PATTERNS,
    PATTERN_REFACTOR,
    PATTERN_REHOST,
    PATTERN_REHOST_CONTAINER,
    PATTERN_REPLATFORM,
    PATTERN_REPURCHASE,
    PATTERN_RETAIN,
    PATTERN_RETIRE,
    PATTERN_REWRITE,
    Question,
    QK_CHOICE,
    QK_VALUE,
    ScanFinding,
    Server,
    STAGE_RETAIN,
    STAGE_RETIRE,
    Wave,
    Workload,
)


ProfileIndex = Dict[str, CodeProfile]


def index_profiles(profiles: Iterable[CodeProfile]) -> ProfileIndex:
    return {p.app_id: p for p in profiles}


# ---------------------------------------------------------------------------
# reconsolidate: merge code-discovered dependencies into the app graph
# ---------------------------------------------------------------------------
def merge_code_deps(workloads: List[Workload],
                    profiles: Iterable[CodeProfile]) -> List[Workload]:
    """Add each app's ``code_deps`` into its ``Workload.depends_on`` (dedup).

    Code-level deps discovered by the executor (HTTP/RPC/queue calls to other
    app_ids) are first-class: they fill edges the static ``apps.csv`` graph
    misses, so the wave DAG reflects what the code actually calls.
    """
    pidx = index_profiles(profiles)
    by_id = {w.app_id: w for w in workloads}
    for w in workloads:
        p = pidx.get(w.app_id)
        if not p:
            continue
        for dep in p.code_deps:
            # only add edges to apps we know about; ignore self-deps
            if dep and dep != w.app_id and dep in by_id and dep not in w.depends_on:
                w.depends_on.append(dep)
    return workloads


# ---------------------------------------------------------------------------
# non-migrating disposition (7R retain / retire)
# ---------------------------------------------------------------------------
def non_migrating_servers(servers: List[Server], workloads: List[Workload],
                          profiles: Iterable[CodeProfile],
                          strategies: Optional[Dict[str, "AppStrategy"]] = None
                          ) -> Tuple[Set[str], Set[str]]:
    """Return (retain_server_ids, retire_server_ids) — servers whose 7R
    disposition is to stay on-prem (retain) or be decommissioned (retire).

    Precedence for an app's disposition (per app_id):
      1. ``strategies[app_id]`` — the AI-assigned 7R (``AppStrategy``), the
         authoritative overlay. Lets a 7R result drive wave exclusion WITHOUT
         first persisting to the DB (pass it straight into the planner).
      2. ``CodeProfile.migration_pattern`` — the executor's scan conclusion
         (legacy: older 7R ``--apply`` wrote retain/retire here). The executor
         vocab is {rehost,replatform,refactor,rewrite}, so retain/retire here
         only appear from that legacy path; treated as a fallback.

    Rule: a server is non-migrating only if EVERY app it belongs to is the
    SAME non-migrating disposition. A server shared by a migrating app and a
    retain app MIGRATES (the migrating app still needs it). Servers with no
    app binding are never retain/retire (they go to the General wave).
    """
    pidx = index_profiles(profiles)
    retain_apps: Set[str] = set()
    retire_apps: Set[str] = set()
    # 1. AI overlay (authoritative) — if present for an app, it wins and the
    #    profile's legacy retain/retire is ignored for that app.
    strat_seen: Set[str] = set()
    if strategies:
        for aid, s in strategies.items():
            strat_seen.add(aid)
            if s.strategy == PATTERN_RETAIN:
                retain_apps.add(aid)
            elif s.strategy == PATTERN_RETIRE:
                retire_apps.add(aid)
    # 2. fallback: legacy retain/retire stored on CodeProfile.migration_pattern
    for aid, p in pidx.items():
        if aid in strat_seen:
            continue                       # AI overlay already decided this app
        if p.migration_pattern == PATTERN_RETAIN:
            retain_apps.add(aid)
        elif p.migration_pattern == PATTERN_RETIRE:
            retire_apps.add(aid)
    # full app set per server (workload.server_ids + server.app_ids)
    srv_to_apps: Dict[str, Set[str]] = {}
    for w in workloads:
        for sid in w.server_ids:
            srv_to_apps.setdefault(sid, set()).add(w.app_id)
    for s in servers:
        for a in (s.app_ids or []):
            srv_to_apps.setdefault(s.id, set()).add(a)
    retain_ids: Set[str] = set()
    retire_ids: Set[str] = set()
    non_mig = retain_apps | retire_apps
    for s in servers:
        apps = srv_to_apps.get(s.id)
        if not apps:
            continue                       # no app binding -> General, not retain/retire
        if apps <= retain_apps:
            retain_ids.add(s.id)
        elif apps <= retire_apps:
            retire_ids.add(s.id)
        elif apps <= non_mig:
            # all apps non-migrating but mixed retain+retire -> decommission
            # (do NOT migrate a host whose every app is staying or retiring)
            retire_ids.add(s.id)
        # else: has at least one migrating app -> migrates
    return retain_ids, retire_ids


def disposition_waves(retain_ids: Set[str], retire_ids: Set[str]) -> List[Wave]:
    """Build the trailing non-migrating disposition waves (7R retain/retire).

    Shared by ``plan_waves`` and ``build_from_policy`` so the stage / sorted
    server ids / depends_on=[] / rationale stay in one place. The wave ``name``
    is a base label ("Retain (on-prem)" / "Retire (decommission)"); callers that
    number their waves (``plan_waves``) prefix it, ``build_from_policy`` uses
    it as-is. Only non-empty dispositions yield a wave.
    """
    out: List[Wave] = []
    if retain_ids:
        out.append(Wave(name="Retain (on-prem)", stage=STAGE_RETAIN,
                        server_ids=sorted(retain_ids), depends_on=[],
                        rationale="7R retain: kept on-prem; not part of the "
                                  "cloud migration sequence."))
    if retire_ids:
        out.append(Wave(name="Retire (decommission)", stage=STAGE_RETIRE,
                        server_ids=sorted(retire_ids), depends_on=[],
                        rationale="7R retire: to decommission; not part of the "
                                  "cloud migration sequence."))
    return out
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}
# ordering for wave sequencing: cheapest/most-certain patterns migrate first;
# non-migrating outcomes (retain/retire) sort last so they don't block the DAG.
# rehost-container sits between rehost and replatform (packaging effort, no
# real code change). repurchase ~ refactor effort (integration work).
_PATTERN_RANK = {
    PATTERN_REHOST: 0,
    PATTERN_REHOST_CONTAINER: 1,
    PATTERN_REPLATFORM: 2,
    PATTERN_REFACTOR: 3,
    PATTERN_REWRITE: 4,            # legacy; heavier than refactor
    PATTERN_REPURCHASE: 3,
    PATTERN_RETAIN: 10,           # not migrated — last
    PATTERN_RETIRE: 11,           # decommission — last
}


def effort_rank(p: Optional[CodeProfile]) -> int:
    return _EFFORT_RANK.get((p.refactor_effort if p else ""), 1)


def pattern_rank(p: Optional[CodeProfile]) -> int:
    return _PATTERN_RANK.get((p.migration_pattern if p else ""), 0)


def order_apps_for_waves(app_ids: Sequence[str],
                         profiles: ProfileIndex) -> List[str]:
    """Stable secondary sort: lower effort / simpler pattern first.

    Used as a tiebreaker within a topological level so cheap rehosts migrate
    ahead of expensive refactors/rewrites, all else (the DAG) equal.
    """
    return sorted(app_ids, key=lambda a: (effort_rank(profiles.get(a)),
                                          pattern_rank(profiles.get(a))))


def cloud_readiness(app_id: str, profiles: ProfileIndex) -> Optional[float]:
    p = profiles.get(app_id)
    return p.cloud_readiness if p else None


def blockers_of(app_id: str, profiles: ProfileIndex) -> List[str]:
    p = profiles.get(app_id)
    return list(p.blockers) if p else []


def wave_rationale_extra(app_id: str, profiles: ProfileIndex) -> str:
    """One-line code-intel annotation for a wave's rationale."""
    p = profiles.get(app_id)
    if not p:
        return ""
    bits = []
    if p.migration_pattern:
        bits.append(f"pattern={p.migration_pattern}")
    if p.refactor_effort:
        bits.append(f"effort={p.refactor_effort}")
    if p.cloud_readiness is not None:
        bits.append(f"readiness={p.cloud_readiness:.2f}")
    if p.blockers:
        bits.append(f"{len(p.blockers)} blocker(s)")
    return "; ".join(bits)


# ---------------------------------------------------------------------------
# wave risk — deterministic basis (the score is NOT from the LLM)
# ---------------------------------------------------------------------------
def wave_risk_basis(wave: Wave, servers: List[Server],
                    matches: List[Match], profiles: Iterable[CodeProfile],
                    db_profiles: Optional[Iterable[DBConversionProfile]] = None
                    ) -> Dict[str, Any]:
    """Deterministic risk basis for a wave — pure, no LLM. Feeds the AI
    runbook/verdict so the *score* is reproducible/auditable and the LLM only
    synthesizes the narrative + runbook.

    Returns {score (0..1), level (low/medium/high), factors (human strings),
    signals (the raw counts the score came from)}.

    F5 — ``db_profiles`` adds a DB-conversion-difficulty signal: any hard (C)
    DB host in the wave bumps risk (hard conversion is a cutover hazard).
    """
    pidx = index_profiles(profiles)
    dbidx: Dict[str, DBConversionProfile] = {d.db_server_id: d
                                            for d in (db_profiles or [])}
    m_by_id = {m.server_id: m for m in (matches or [])}
    by_id = {s.id: s for s in servers}
    members = [by_id[sid] for sid in wave.server_ids if sid in by_id]
    n = len(members)

    crit = {"high": 0, "medium": 0, "low": 0, "": 0}
    high_util = 0
    max_util = 0.0
    products: Dict[str, int] = {}
    readiness_vals: List[float] = []
    blocker_count = 0
    pattern_mix: Dict[str, int] = {}
    hard_db = 0
    db_man_days = 0.0
    for s in members:
        crit[(s.business_criticality or "").lower()] = crit.get((s.business_criticality or "").lower(), 0) + 1
        u = s.utilization
        util = max(u.cpu_p95 or 0, u.mem_p95 or 0, u.disk_used_pct or 0)
        max_util = max(max_util, util)
        if util >= 80:
            high_util += 1
        m = m_by_id.get(s.id)
        if m and m.target and m.target.product:
            products[m.target.product] = products.get(m.target.product, 0) + 1
        # F5 — db_profiles keyed by stable host identity (hostname), not Server.id
        dbp = None
        for k in (s.hostname.lower().strip(), s.identity_key, s.id):
            dbp = dbidx.get(k)
            if dbp:
                break
        if dbp:
            if dbp.difficulty == DB_DIFFICULTY_C:
                hard_db += 1
            db_man_days += float(dbp.est_man_days or 0.0)
        for a in (s.app_ids or []):
            p = pidx.get(a)
            if not p:
                continue
            if p.cloud_readiness is not None:
                readiness_vals.append(p.cloud_readiness)
            blocker_count += len(p.blockers or [])
            if p.migration_pattern:
                pattern_mix[p.migration_pattern] = pattern_mix.get(p.migration_pattern, 0) + 1
    min_readiness = min(readiness_vals) if readiness_vals else None
    dep_depth = len(wave.depends_on or [])
    is_cutover = (wave.stage == "4_cutover")

    # score: additive over signals, capped 1.0. Calibrated so a low-crit
    # landing-zone wave is low, a high-criticality cutover is high.
    score = 0.10
    factors: List[str] = []
    if crit.get("high", 0) > 0:
        score += 0.30; factors.append(f"{crit['high']} high-criticality server(s)")
    elif crit.get("medium", 0) > 0:
        score += 0.10; factors.append(f"{crit['medium']} medium-criticality")
    if high_util > 0:
        score += 0.10; factors.append(f"{high_util} high-utilization (>=80%)")
    if min_readiness is not None and min_readiness < 0.5:
        score += 0.20; factors.append(f"low code readiness ({min_readiness:.2f})")
    if blocker_count > 0:
        score += 0.10; factors.append(f"{blocker_count} code blocker(s)")
    if hard_db > 0:
        score += 0.15; factors.append(f"{hard_db} hard-DB-conversion host(s) (grade C)")
    if is_cutover:
        score += 0.15; factors.append("cutover (traffic-switch) stage")
    if dep_depth >= 3:
        score += 0.05; factors.append(f"depends on {dep_depth} prior waves")
    score = round(min(1.0, score), 2)
    level = "low" if score < 0.4 else ("medium" if score < 0.7 else "high")
    if not factors:
        factors.append("no elevated-risk signals")

    return {
        "score": score, "level": level, "factors": factors,
        "signals": {
            "n_servers": n, "by_criticality": {k: v for k, v in crit.items() if k and v},
            "high_util_count": high_util, "max_utilization": round(max_util, 1),
            "target_products": products, "min_readiness": min_readiness,
            "blocker_count": blocker_count, "pattern_mix": pattern_mix,
            "dep_depth": dep_depth, "is_cutover": is_cutover, "stage": wave.stage,
            "hard_db_count": hard_db, "db_man_days": round(db_man_days, 1),
        },
    }


# ---------------------------------------------------------------------------
# migration strategy / match enrichment
# ---------------------------------------------------------------------------
def enrich_match(match: Match, profile: Optional[CodeProfile],
                  assessment_confidence: float = 1.0) -> Match:
    """Adjust a Match using the app's code profile + data-coverage confidence.

    * Low cloud readiness or any blocker → lower confidence.
    * ``rewrite`` → confidence floor 0.2 (not a real migration target).
    * ``retain``/``retire`` → the 7R non-migrating outcomes: confidence floored
      and a clear "do not migrate" note, since the strategy says this app stays
      on-prem or is decommissioned.
    * Blockers + pattern are appended to rationale so the strategy explains
      *why* an app is risky to lift-and-shift.
    * F4 — the base rule confidence is finally scaled by ``assessment_confidence``
      (data-coverage), so a thinly-known host can't be reported as
      high-confidence. Default 1.0 = identity (no coverage signal available).
    """
    if not profile and assessment_confidence >= 1.0:
        return match
    notes = []
    pat = profile.migration_pattern if profile else ""
    # F9 — runtime-derived profiles (no source) are inferred scaffolds: cap
    # confidence so the operator sees the scaffold is a guess, not a scan.
    if is_runtime_derived(profile):
        match.confidence = min(match.confidence, RUNTIME_CONFIDENCE_CAP)
        notes.append(f"runtime-derived (no source) — confidence capped at {RUNTIME_CONFIDENCE_CAP:.2f}, operator confirm required")
    if pat == PATTERN_REWRITE:
        match.confidence = min(match.confidence, 0.2)
        notes.append("code assessment: rewrite (not a lift-and-shift candidate)")
    if pat in NON_MIGRATING_PATTERNS:
        # retain/retire are deliberate non-migration outcomes — flag hard
        match.confidence = min(match.confidence, 0.1)
        label = "keep on-prem (retain)" if pat == PATTERN_RETAIN else "decommission (retire)"
        notes.append(f"7R strategy: {label} — do not migrate")
    if profile and profile.blockers:
        # each blocker can shave confidence; cap the total dip at 0.35
        dip = min(0.12 * len(profile.blockers), 0.35)
        match.confidence = max(0.05, match.confidence - dip)
        notes.append("blockers: " + "; ".join(profile.blockers))
    elif profile and profile.cloud_readiness is not None and profile.cloud_readiness < 0.5:
        match.confidence = max(0.05, match.confidence - 0.15)
        notes.append(f"low cloud readiness ({profile.cloud_readiness:.2f})")
    # call out anything beyond a plain rehost (rehost-container is still mostly
    # packaging, so only flag the genuinely code-changing strategies)
    if pat and pat not in (PATTERN_REHOST, PATTERN_REHOST_CONTAINER) \
            and pat not in NON_MIGRATING_PATTERNS:
        notes.append(f"requires {pat}")
    # F4 — scale by data-coverage confidence (identity when 1.0 / no signal).
    if assessment_confidence < 1.0:
        match.confidence = max(0.05, match.confidence * assessment_confidence)
        notes.append(f"data-coverage {assessment_confidence:.2f} → confidence {match.confidence:.2f}")
    if notes:
        tag = f"[code] {'. '.join(notes)}"
        match.rationale = (match.rationale + " " + tag).strip() if match.rationale else tag
    return match


# ---------------------------------------------------------------------------
# F9 — runtime-derived (no-source) containerization path
# ---------------------------------------------------------------------------
SOURCE_REPO = "repo"
SOURCE_RUNTIME = "runtime-derived"
# runtime-derived profiles are inferred from telemetry, not source — cap
# confidence so the operator knows the scaffold is a guess, not a scan.
RUNTIME_CONFIDENCE_CAP = 0.5


def is_runtime_derived(profile: Optional[CodeProfile]) -> bool:
    """True when a CodeProfile was produced by the no-source containerization
    path (F9), not a repo scan. Such profiles carry lower confidence and force
    an operator-confirm Question before ``modify``."""
    return bool(profile and (profile.source or "") == SOURCE_RUNTIME)


def runtime_confirm_question(app_id: str, profile: Optional[CodeProfile],
                             job_id: str = "") -> Optional[Question]:
    """The operator-confirm gate for the no-source path (F9).

    A runtime-derived profile is a scaffold *inferred from telemetry*, not a
    scan. Before the executor writes it (``modify`` mode=execute), the operator
    must confirm the inferred Dockerfile is right. Returns a ``Question`` (or
    None if the profile is repo-sourced / missing — no gate needed)."""
    if not is_runtime_derived(profile):
        return None
    return Question(
        app_id=app_id, job_id=job_id, kind=QK_CHOICE,
        prompt=(f"App {app_id} has no source repo — the Dockerfile scaffold was "
                f"inferred from runtime telemetry (process/port/software). "
                f"Confirm the scaffold is correct before the executor writes it?"),
        options=["confirmed — proceed with modify", "reject — needs manual review"],
        context={"source": SOURCE_RUNTIME, "app_id": app_id,
                 "summary": getattr(profile, "summary", "")},
    )


# ---------------------------------------------------------------------------
# F5 — DB heterogeneous conversion enrichment
# ---------------------------------------------------------------------------
# Difficulty grade -> confidence dip + pattern forcing. A = clean lift, B =
# some manual review (keep confidence, note the effort), C = hard PL/SQL /
# engine-specific features -> force replatform and a real confidence dip so
# the wave planner defers the host. Mirrors AWS DMS SC quality scoring +
# Azure Ora2Pg A–C grading: the grade is the deterministic signal the LLM
# only narrates.
_DB_DIFFICULTY_DIP = {
    DB_DIFFICULTY_A: 0.0,    # mostly auto-convertible — no confidence penalty
    DB_DIFFICULTY_B: 0.15,   # manual review — modest dip
    DB_DIFFICULTY_C: 0.40,   # hard conversion — strong dip, force replatform
}
_DB_DIFFICULTY_RANK = {DB_DIFFICULTY_A: 0, DB_DIFFICULTY_B: 1, DB_DIFFICULTY_C: 2}


def db_difficulty_rank(d: Optional[DBConversionProfile]) -> int:
    """0/1/2 for A/B/C (None / unknown = 0). Used by wave risk + ordering."""
    if not d or not d.difficulty:
        return 0
    return _DB_DIFFICULTY_RANK.get(d.difficulty, 0)


def enrich_match_db(match: Match, db_profile: Optional[DBConversionProfile]) -> Match:
    """Adjust a DB-host Match using the heterogeneous conversion assessment (F5).

    * Hard conversion (C) → strong confidence dip + force
      ``migration_pattern=replatform`` (a direct rehost would carry the
      unconverted PL/SQL); append the conversion blockers to rationale.
    * Medium (B) → modest dip + a "manual review" note; pattern unchanged.
    * Easy (A) → no dip; just annotate that conversion was assessed.
    * ``reverse_replication=True`` is surfaced in rationale as the rollback
      safety net (consumed by F6 for db-kind cutover revert).

    Pure: returns the same ``match`` object (mutated in place, like
    ``enrich_match``) so callers can chain ``enrich_match`` → ``enrich_match_db``.
    """
    if not db_profile:
        return match
    notes: List[str] = []
    dip = _DB_DIFFICULTY_DIP.get(db_profile.difficulty, 0.0)
    if dip > 0:
        match.confidence = max(0.05, match.confidence - dip)
        notes.append(f"DB conversion difficulty={db_profile.difficulty} "
                     f"(confidence -{dip:.2f})")
    if db_profile.difficulty == DB_DIFFICULTY_C:
        # hard conversion is not a lift-and-shift — force replatform so the
        # host lands in a later, code-touching wave instead of the data wave.
        match.method = "hybrid"
        notes.append("hard DB conversion → replatform (not a direct rehost)")
    if (db_profile.est_man_days or 0) > 0:
        notes.append(f"est. {db_profile.est_man_days:.0f} man-day conversion")
    if db_profile.review_objects:
        notes.append(f"{len(db_profile.review_objects)} object(s) need manual review")
    if db_profile.blockers:
        notes.append("DB blockers: " + "; ".join(db_profile.blockers))
    if db_profile.reverse_replication:
        notes.append("reverse replication enabled (cutover rollback net)")
    if notes:
        tag = f"[db] {'. '.join(notes)}"
        match.rationale = (match.rationale + " " + tag).strip() if match.rationale else tag
    return match


# ---------------------------------------------------------------------------
# ongoing interaction: executor pulls context / asks to resolve a value
# ---------------------------------------------------------------------------
# The executor is not fire-and-forget — while it works it needs to ask
# idc-migrate things: "what does this app depend on?", "what cloud target did
# its DB match to?", and crucially "I just found a hardcoded IP that wasn't in
# the upfront changes list — what should it become?". These helpers answer
# those questions from the estate (workloads + matches) + operator overrides.
# They are pure (no DB) so the backend routes are thin and they are unit-tested
# directly.

# what migration target product each CHANGE_KIND tends to parameterize toward
# — used to suggest a placeholder *form* when the operator didn't give a value
_KIND_TARGET = {
    "ip": "host",          # any IP → some cloud host
    "host": "host",
    "endpoint": "endpoint",
    "password": "secret",
    "secret": "secret",
    "config": "config",
    "runtime": "runtime",
}

# default placeholder per kind, refined by the matched target product below
_KIND_PLACEHOLDER = {
    "ip": "${HOST}", "host": "${HOST}", "endpoint": "${ENDPOINT}",
    "password": "${SECRET}", "secret": "${SECRET}", "config": "${ENV}",
    "runtime": "${RUNTIME}",
}

# matched target product → a more specific placeholder hint. lets the executor
# emit ${DB_HOST} for a CDB match instead of a generic ${HOST}.
_PRODUCT_PLACEHOLDER = {
    "CDB": "${DB_HOST}", "redis": "redis", "Redis": "redis",
    "CVM": "${HOST}", "BM": "${HOST}", "TKE": "${HOST}",
    "CFS": "${NFS_HOST}", "COS": "${COS_ENDPOINT}",
    "EMR": "${EMR_MASTER}",
}


def app_targets(workloads: Sequence[Workload], matches: Iterable[Match],
                app_id: str) -> List[Match]:
    """Return the migration matches for an app's servers (blast-radius context).

    The executor uses this while modifying code: knowing the app's DB server
    matched to ``CDB`` (region=shanghai) tells it the connection should
    parameterize to a CDB-style host, not a bare-IP JDBC string.
    """
    w = next((x for x in workloads if x.app_id == app_id), None)
    if w is None:
        return []
    sids = set(w.server_ids)
    return [m for m in matches if m.server_id in sids]


def resolve_value(kind: str, old: str,
                  matches: Optional[Sequence[Match]] = None,
                  overrides: Optional[Dict[str, str]] = None
                  ) -> Dict[str, Any]:
    """Answer "what should this old value become?" for the executor.

    Resolution order (most-trusted first):
      1. operator override (``old → new``) — explicit, high confidence;
      2. match-derived placeholder *form* (e.g. a CDB match → ``${DB_HOST}``) —
         a suggestion, low confidence (idc-migrate knows the *form*, not the
         literal new hostname; the literal is a cutover-time artifact);
      3. generic placeholder per kind — lowest confidence, a last resort;
      4. none of the above → ``new=None, source="unknown"``.

    Honesty rule: idc-migrate never invents a concrete new endpoint. When it
    can only suggest a *form*, ``confidence`` is low and ``source`` says so, so
    the executor knows to confirm with the operator before applying.
    """
    overrides = overrides or {}
    if old and old in overrides:
        return {"old": old, "new": overrides[old], "source": "override",
                "confidence": 1.0}
    ph = _KIND_PLACEHOLDER.get(kind, "${ENV}")
    # refine by matched target product if we have one for the relevant kind
    src = "default"
    conf = 0.2
    if matches:
        products = {(m.target.product or "").strip() for m in matches}
        for prod in products:
            if prod and prod in _PRODUCT_PLACEHOLDER and kind in ("ip", "host", "endpoint"):
                ph = _PRODUCT_PLACEHOLDER[prod]
                src = "match-derived"
                conf = 0.4
                break
    return {"old": old, "new": ph, "source": src, "confidence": conf}


def question_for_change(app_id: str, change: Dict[str, Any],
                        job_id: str = "") -> Optional[Question]:
    """Turn an *unresolved* change item into a Question for the operator.

    The glue between ``build_change_spec`` and the question channel: when a
    change item has empty ``old``/``new`` (idc-migrate couldn't derive the
    literal or the replacement), it should become a blocking question instead
    of a guess. Resolved items (both ``old`` and ``new`` present) return
    ``None`` — nothing to ask.

    Kind selection:
      * ``old`` empty → ``value`` ("what literal should I look for / replace?")
      * ``old`` present but ``new`` empty → ``value`` ("what should <old> become?")
        with the obvious placeholder as a ``choice`` option so the operator can
        one-click accept the suggestion;
      * a secret with empty ``new`` → ``confirm``-flavored ``value`` (the prompt
        flags it as a secret so the operator doesn't paste the literal back).
    """
    old = (change.get("old") or "").strip()
    new = (change.get("new") or "").strip()
    if old and new:
        return None  # fully resolved — nothing to ask
    file = change.get("file", "")
    line = change.get("line", 0)
    cat = change.get("category", "")
    loc = f"{file}:{line}" if file else "(location unknown)"
    kind = change.get("kind", "other")
    ctx = {"category": cat, "kind": kind, "file": file, "line": line,
           "old": old, "new": new, "title": change.get("title", ""),
           "evidence": change.get("evidence", "")}

    if not old:
        prompt = (f"I couldn't extract the literal to replace in {loc} "
                  f"({cat}). What exactly should I find and replace there?")
        return Question(app_id=app_id, job_id=job_id, kind=QK_VALUE,
                        prompt=prompt, context=ctx)

    # old known, new unknown → suggest the default placeholder as a choice
    suggested = _KIND_PLACEHOLDER.get(kind, "${ENV}")
    options = [suggested]
    if cat == "secrets_in_repo":
        prompt = (f"Found a committed secret in {loc}. What secret ref should "
                  f"replace {old!r}? (don't paste the new secret itself — give "
                  f"a ref like ${{DB_PASSWORD}})")
    else:
        prompt = (f"What should {old!r} in {loc} ({cat}) be replaced with?")
    return Question(app_id=app_id, job_id=job_id, kind=QK_CHOICE,
                    prompt=prompt, options=options, context=ctx)


# ---------------------------------------------------------------------------
# modify: turn a CodeProfile into concrete, executor-ready change instructions
# ---------------------------------------------------------------------------
# The modify request used to send only {app_id, repo_url, branch, mode, scope}
# — i.e. "change this app, limited to these categories" — leaving the executor
# to re-scan the repo and guess *which file/line, which literal, which value*.
# ``build_change_spec`` fills that gap: it joins the profile's required_changes
# with its findings (by file+line) to emit one item per change, each carrying
# the literal to replace (``old``) and the replacement (``new``).
#
# Honesty rule: where ``old``/``new`` can't be derived from the profile, the
# field is left empty with a ``note`` — the executor then knows it must
# resolve (or skip) that item, instead of inventing a change. We never put a
# guess in ``old``/``new``; the whole point of this struct is to remove guesses.

# which CHANGE_KIND each finding category maps to by default
_CATEGORY_KIND = {
    "hardcoded_ip": "ip",
    "service_discovery": "config",
    "db_connection": "endpoint",
    "stateful_local": "config",
    "scheduled_job": "runtime",
    "secrets_in_repo": "secret",
    "baremetal_assumption": "config",
    "network_dependency": "endpoint",
    "os_dependency": "config",
    "legacy_runtime": "runtime",
    "config_coupling": "config",
}

# default replacement placeholder per category — the *form* of the new value
# (an env/secret ref), not a literal endpoint. idc-migrate generally can't know
# the concrete new CDB hostname (that's a cutover-time artifact); it CAN tell
# the executor "parameterize this hardcoded host into ${DB_HOST}", and the
# operator can pin exact names via overrides.
_DEFAULT_NEW = {
    "hardcoded_ip": "${HOST}",
    "service_discovery": "${SERVICE_ENDPOINT}",
    "db_connection": "${DB_HOST}",
    "stateful_local": "${STATE_DIR}",
    "scheduled_job": "${SCHEDULER}",          # cloud job scheduler (e.g. TKE cron)
    "secrets_in_repo": "${SECRET}",
    "baremetal_assumption": "${HOST_IDENTITY}",
    "network_dependency": "${ENDPOINT}",
    "os_dependency": "${PLATFORM}",
    "legacy_runtime": "${RUNTIME}",
    "config_coupling": "${ENV}",
}

_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOSTPORT_RE = re.compile(r"\b([A-Za-z0-9.\-]+):(\d{1,5})\b")
_SECRET_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*"
    r"['\"]?([^\s'\"#]+)['\"]?", re.IGNORECASE)


def _extract_old(category: str, evidence: str) -> str:
    """Best-effort pull the literal token to replace out of a finding's evidence.

    Returns "" if nothing confident matches (caller leaves ``old`` empty with a
    note — never returns a guess).
    """
    ev = evidence or ""
    if not ev:
        return ""
    if category == "secrets_in_repo":
        m = _SECRET_RE.search(ev)
        return m.group(1) if m else ""
    if category in ("db_connection", "network_dependency"):
        m = _HOSTPORT_RE.search(ev)
        if m:
            return f"{m.group(1)}:{m.group(2)}"
        m = _IP_RE.search(ev)
        return m.group(0) if m else ""
    if category == "hardcoded_ip":
        m = _IP_RE.search(ev)
        return m.group(0) if m else ""
    # other categories: no reliable single-token extractor — leave to executor
    return ""


def build_change_spec(profile: Optional[CodeProfile],
                      scope: Optional[Sequence[str]] = None,
                      overrides: Optional[Dict[str, str]] = None
                      ) -> ChangeSpec:
    """Build concrete change instructions for a modify request.

    ``scope``    — restrict to these finding categories (None = all required_changes).
    ``overrides``— operator-supplied ``old → new`` map. Applied to items whose
                   ``old`` matches a key: the key's value becomes the item's
                   ``new`` (lets the operator pin "10.0.4.20 → ${DB_HOST}" or
                   "hunter2 → ${DB_PASSWORD}"). Unmatched overrides are reported
                   in ``ChangeSpec.notes`` so the operator sees the scan didn't
                   flag that token.

    Each emitted item joins ``required_changes`` (the human change list) with
    ``findings`` (the evidence trail) by ``(file, line)`` so the executor gets
    both *what to change* and *where the evidence is*.
    """
    overrides = dict(overrides or {})
    if profile is None:
        return ChangeSpec(app_id="", changes=[], notes=[
            "no CodeProfile for this app — run scan/comb first, or the executor "
            "must scan inline before modifying"])
    scope_set = set(scope) if scope is not None else None

    # index findings by (file, line) so required_changes can pull evidence/line
    findings_by_loc: Dict[tuple, ScanFinding] = {}
    no_loc_findings: List[ScanFinding] = []
    for f in (profile.findings or []):
        if f.file and f.line:
            findings_by_loc[(f.file, f.line)] = f
        else:
            no_loc_findings.append(f)

    changes: List[Dict] = []
    used_overrides = set()
    seen_keys = set()  # dedup by (category, file, line, old)

    def _emit(category: str, file: str, line: int, title: str,
              evidence: str, old: str, new: str, effort: str,
              description: str, note: str = "") -> None:
        key = (category, file, line, old)
        if key in seen_keys:
            return
        seen_keys.add(key)
        item = {
            "category": category,
            "kind": _CATEGORY_KIND.get(category, "other"),
            "file": file,
            "line": line,
            "title": title,
            "evidence": evidence[:512],   # honor the 512-char evidence cap
            "old": old,
            "new": new,
            "effort": effort or "medium",
            "description": description,
            "note": note,
        }
        # apply operator override: if this item's old matches, replace its new
        if old and old in overrides:
            item["new"] = overrides[old]
            item["note"] = (note + " " if note else "") + "new value from operator override"
            used_overrides.add(old)
        changes.append(item)

    # 1. required_changes drive the change list (these are the actionable ones)
    for rc in (profile.required_changes or []):
        cat = rc.get("category", "")
        if scope_set is not None and cat not in scope_set:
            continue
        file = rc.get("file", "") or ""
        line = int(rc.get("line") or 0)
        title = rc.get("title", "") or rc.get("description", "") or ""
        desc = rc.get("description", "") or ""
        effort = rc.get("effort", "medium") or "medium"
        # try to attach evidence + a better line from a matching finding
        f = findings_by_loc.get((file, line))
        if f is None and no_loc_findings:
            f = next((x for x in no_loc_findings if x.category == cat), None)
        evidence = f.evidence if f else ""
        if not line and f:
            line = f.line
        old = _extract_old(cat, evidence)
        new = _DEFAULT_NEW.get(cat, "${ENV}")
        note = ""
        if not old:
            note = "old literal not derivable from evidence; executor must locate it"
        # for secrets, never ship a guessed old; keep old="" if extraction failed
        if cat == "secrets_in_repo" and not old:
            old = ""
            note = "secret literal not extracted (redacted); executor must locate by file/line"
        _emit(cat, file, line, title, evidence, old, new, effort, desc, note)

    # 2. findings whose category is in scope but not in required_changes still
    #    matter (e.g. hardcoded_ip findings the comb step didn't fold into a
    #    required_change) — surface them so the executor addresses them too
    rc_locs = {(rc.get("file", ""), int(rc.get("line") or 0), rc.get("category", ""))
               for rc in (profile.required_changes or [])}
    for f in (profile.findings or []):
        if scope_set is not None and f.category not in scope_set:
            continue
        if (f.file or "", f.line or 0, f.category) in rc_locs:
            continue  # already emitted via required_changes (same category)
        old = _extract_old(f.category, f.evidence)
        new = _DEFAULT_NEW.get(f.category, "${ENV}")
        note = ""
        if not old:
            note = "old literal not derivable from evidence; executor must locate it"
        if f.category == "secrets_in_repo" and not old:
            old = ""
            note = "secret literal not extracted (redacted); executor must locate by file/line"
        _emit(f.category, f.file or "", f.line or 0,
              f.message or f.category, f.evidence or "", old, new,
              "medium", f.remediation or "", note)

    notes = []
    unmatched = [k for k in overrides if k not in used_overrides]
    if unmatched:
        notes.append(
            f"{len(unmatched)} override(s) had no matching finding and were ignored: "
            + ", ".join(repr(k) for k in unmatched[:8])
            + (" …" if len(unmatched) > 8 else ""))
    if not changes:
        notes.append("no required_changes or in-scope findings in the profile — "
                     "nothing concrete to change; executor should re-scan or skip")

    return ChangeSpec(app_id=profile.app_id, changes=changes, notes=notes)