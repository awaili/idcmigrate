"""Named-executor registry — idc-migrate can drive N external executors, not one.

The ``default`` executor is seeded from the ``IDC_EXECUTOR_*`` env overlaid with
the legacy DB ``executor_*`` keys (so a single-executor deployment keeps working
with zero config change, and the existing ``/api/executor/config`` panel still
manages it). Additional **named** executors live in the ``executors`` JSON config
row. Each trigger picks an executor by id; the marked-default (or the default)
one is used when no id is given.

Push direction (executor → idc-migrate) is unchanged: every executor pushes to
the SAME idc-migrate public URL (``public_url`` is global) and authenticates
with its own token; idc-migrate accepts any registered token (see
``valid_tokens``), so pushes from any configured executor validate.

Internal helpers return the token in each entry (for resolution + bearer
validation). The API layer MUST strip ``token`` before returning a registry to
the browser — only ``token_set`` is public.
"""
from __future__ import annotations

import json
import dataclasses
import secrets
from typing import Any, Dict, List, Optional

from ..config import Settings

DEFAULT_ID = "default"
_REGISTRY_KEY = "executors"
_ENROLL_SECRET_KEY = "enroll_secret"
_TRUTHY = ("1", "true", "yes", "on")
_LEGACY_KEYS = ("executor_url", "executor_token", "executor_enabled",
                "executor_timeout")

# Approval lifecycle for self-registered executors. A named executor that
# self-enrolls (POST /api/executors/register) lands PENDING with no token; it
# cannot claim or push until an operator APPROVES it, at which point
# idc-migrate mints its bearer token and hands it back to the operator to
# relay to the executor out-of-band. The default executor and any executor an
# operator creates directly via upsert() are APPROVED by construction
# (operator-authored), so legacy rows without a ``status`` field are read as
# approved (back-compat).
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"

# Self-registered executor id must be a safe slug (so it can't smuggle HTML /
# path traversal into the UI or the registry JSON). 1-64 chars.
import re as _re
_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Cap on concurrent PENDING enrollments so an open-registration endpoint
# (no enroll secret set) can't be spammed into unbounded DB clutter. Pending
# rows are inert (no token → can't claim/push), so this is a tidiness guard,
# not a security control; the approval gate is the security control.
_PENDING_CAP = 50


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUTHY


def _registry_raw(store) -> List[Dict[str, Any]]:
    """The named (non-default) executors from the ``executors`` JSON config row."""
    raw = store.get_config(_REGISTRY_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: List[Dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            if str(it["id"]) == DEFAULT_ID:
                continue   # default is sourced from the legacy keys, not the list
            try:
                timeout = int(it.get("timeout") or 600)
            except (TypeError, ValueError):
                timeout = 600
            out.append({
                "id": str(it["id"]),
                "url": (it.get("url") or "").strip(),
                "token": it.get("token") or "",
                "enabled": _to_bool(it.get("enabled", True)),
                # legacy rows predate the status field; an operator-authored
                # entry is approved by construction, and a self-enrolled one
                # always carries status="pending", so default to approved.
                "status": it.get("status") or STATUS_APPROVED,
                "timeout": timeout,
                "default": False,
            })
    return out


def _default_entry(store, settings: Settings) -> Dict[str, Any]:
    """The default executor: env ``IDC_EXECUTOR_*`` overlaid with the legacy DB
    ``executor_*`` keys (the existing single-executor config)."""
    base = {k: getattr(settings, k) for k in _LEGACY_KEYS}
    ov = store.get_config_map(_LEGACY_KEYS)
    if "executor_url" in ov:
        base["executor_url"] = ov["executor_url"]
    if "executor_token" in ov:
        base["executor_token"] = ov["executor_token"]
    if "executor_enabled" in ov:
        base["executor_enabled"] = _to_bool(ov["executor_enabled"])
    if "executor_timeout" in ov:
        try:
            base["executor_timeout"] = int(ov["executor_timeout"])
        except (TypeError, ValueError):
            pass
    return {
        "id": DEFAULT_ID,
        "url": (base["executor_url"] or "").strip(),
        "token": base["executor_token"] or "",
        "enabled": bool(base["executor_enabled"]),
        "status": STATUS_APPROVED,   # the default is operator-managed, never pending
        "timeout": int(base["executor_timeout"] or 600),
        "default": True,
    }


def list_executors(store, settings: Settings) -> List[Dict[str, Any]]:
    """Full registry: ``[default] + named``. Each entry carries ``token``
    (internal resolution only — the API layer must strip it before returning)."""
    return [_default_entry(store, settings)] + _registry_raw(store)


def _global_public_url(store, settings: Settings) -> str:
    """The single idc-migrate push target every executor pushes to (env
    ``IDC_PUBLIC_URL`` overlaid with the DB ``public_url`` row)."""
    pub = settings.public_url or ""
    ov = store.get_config("public_url")
    if ov is not None:
        pub = ov
    return (pub or "").strip()


def _claimable(entry: Dict[str, Any]) -> bool:
    """An executor's bearer token is accepted on the push direction iff it is
    APPROVED and holds a token. A PENDING self-enrollment has no token (so it
    is blocked by the empty-token check already), but the status gate makes
    the intent explicit and defends against any future code path that writes a
    token before approval. The ``enabled`` toggle is NOT considered here: it is
    an operator on/off switch already enforced at the claim endpoint (409 when
    disabled), and push-back is the executor reporting work already in flight —
    so a disabled-but-approved executor's pushes still validate (unchanged from
    the pre-self-registration behavior)."""
    return (entry.get("status", STATUS_APPROVED) == STATUS_APPROVED
            and bool(entry.get("token")))


def resolve(store, settings: Settings, executor_id: Optional[str]) -> Settings:
    """Return a Settings copy overlaid with the selected executor's
    url/token/enabled/timeout + the global ``public_url``. ``executor_id``
    None/''/'default' → the default executor. Raises ``KeyError`` if the named
    executor isn't registered OR is still PENDING (a pending executor can't be
    targeted — it has no token to claim with; the caller maps KeyError → 404,
    and the API layer upgrades a pending KeyError to a 409 'pending approval').
    """
    regs = list_executors(store, settings)
    eid = (executor_id or "").strip()
    if not eid or eid == DEFAULT_ID:
        chosen = regs[0]
    else:
        chosen = next((e for e in regs if e["id"] == eid), None)
        if chosen is None:
            raise KeyError(eid)
        if chosen.get("status", STATUS_APPROVED) == STATUS_PENDING:
            raise KeyError(eid)   # registered but not yet approved
    return dataclasses.replace(
        settings,
        executor_url=chosen["url"],
        executor_token=chosen["token"],
        executor_enabled=chosen["enabled"],
        executor_timeout=chosen["timeout"],
        public_url=_global_public_url(store, settings),
    )


def valid_tokens(store, settings: Settings) -> List[str]:
    """Every bearer token accepted on the push direction (any APPROVED,
    enabled, token-bearing executor may push back to idc-migrate; the
    default's token is included). PENDING self-enrollments are excluded."""
    return [e["token"] for e in list_executors(store, settings)
            if _claimable(e)]


def resolve_by_token(store, settings: Settings, token: str) -> Optional[str]:
    """Map a bearer token to its executor id (constant-time compare). Returns
    the executor id ("default" for the default executor) or None when no
    APPROVED executor's token matches. A PENDING self-enrollment has no token,
    so it can never resolve here — an unapproved executor polling claim gets a
    401 exactly as if its token were unknown."""
    if not token:
        return None
    for e in list_executors(store, settings):
        if not _claimable(e):
            continue
        et = e.get("token") or ""
        if et and secrets.compare_digest(token, et):
            return e["id"]
    return None


def public_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the token (keep only ``token_set``) for API/UI consumption, and
    surface the approval lifecycle as ``approval`` (pending|approved)."""
    out = dict(entry)
    out["token_set"] = bool(out.pop("token", ""))
    out["approval"] = out.get("status", STATUS_APPROVED)
    return out


def upsert(store, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Add/update a NAMED executor (id != default) in the registry. Returns the
    normalized entry (with ``default=False``). The default executor is managed
    via the legacy ``/api/executor/config`` endpoint, not here."""
    eid = str(entry.get("id") or "").strip()
    if not eid:
        raise ValueError("executor id required")
    if eid == DEFAULT_ID:
        raise ValueError("the default executor is managed via /api/executor/config")
    url = (entry.get("url") or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must be http(s)://...")
    try:
        timeout = int(entry.get("timeout") or 600)
    except (TypeError, ValueError):
        timeout = 600
    norm = {
        "id": eid,
        "url": url,
        "token": entry.get("token") or "",
        "enabled": _to_bool(entry.get("enabled", True)),
        # operator-authored → approved by construction (the operator is the
        # trust root; upsert is the "I already have a token" manual path).
        "status": STATUS_APPROVED,
        "timeout": timeout,
    }
    items = _registry_raw(store)
    items = [e for e in items if e["id"] != eid]
    items.append(norm)
    store.set_config(_REGISTRY_KEY, json.dumps(items))
    norm["default"] = False
    return norm


def delete(store, executor_id: str) -> bool:
    """Remove a NAMED executor (whether pending or approved — 'reject' for a
    pending self-enrollment, 'remove' for an approved one). Returns False if it
    wasn't there; raises ``ValueError`` for the default (can't delete it)."""
    eid = (executor_id or "").strip()
    if not eid or eid == DEFAULT_ID:
        raise ValueError("cannot delete the default executor")
    items = _registry_raw(store)
    new = [e for e in items if e["id"] != eid]
    if len(new) == len(items):
        return False
    store.set_config(_REGISTRY_KEY, json.dumps(new))
    return True


# ---------------------------------------------------------------------------
# Self-registration → approval lifecycle (Option B: idc-migrate mints the
# token on approval; the executor never holds a secret it invented).
# ---------------------------------------------------------------------------

def enroll_secret(store, settings: Settings) -> str:
    """The optional enrollment secret gate (env ``IDC_ENROLL_SECRET`` overlaid
    with the DB ``enroll_secret`` row). When non-empty, an executor must send
    ``X-Enroll-Secret`` matching this to register at all. When empty,
    self-registration is open — but every enrollment still lands PENDING and
    inert until an operator approves, so open enrollment can only clutter the
    pending list, not claim work or push."""
    sec = settings.enroll_secret or ""
    ov = store.get_config(_ENROLL_SECRET_KEY)
    if ov is not None:
        sec = ov
    return (sec or "").strip()


def _mint_token() -> str:
    """A high-entropy bearer token minted server-side on approval."""
    return "ex-" + secrets.token_urlsafe(32)


def register(store, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Self-enroll a NAMED executor as PENDING (no token, not claimable).

    Idempotent on a PENDING enrollment: re-registering the same id refreshes
    its ``url``/``timeout`` and stays pending. Re-registering an id that is
    already APPROVED is rejected (ValueError → 409): an attacker who knows an
    approved executor's id cannot use registration to alter it (the approved
    entry keeps its token; only the operator can rotate it). Returns the
    pending entry (no token).
    """
    eid = str(entry.get("id") or "").strip()
    if not eid:
        raise ValueError("executor id required")
    if eid == DEFAULT_ID:
        raise ValueError("the default executor is managed via /api/executor/config")
    if not _ID_RE.match(eid):
        raise ValueError("executor id must be 1-64 chars of [A-Za-z0-9._-], "
                         "starting alphanumeric")
    url = (entry.get("url") or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must be http(s://)... or empty")
    try:
        timeout = int(entry.get("timeout") or 600)
    except (TypeError, ValueError):
        timeout = 600

    items = _registry_raw(store)
    existing = next((e for e in items if e["id"] == eid), None)
    if existing is not None and existing.get("status") == STATUS_APPROVED:
        raise ValueError(f"executor {eid!r} is already approved; "
                         "ask the operator to rotate its token or pick a new id")
    # cap pending clutter (only counts pending, so an approved estate doesn't
    # consume the budget)
    pending_n = sum(1 for e in items if e.get("status") == STATUS_PENDING)
    if existing is None and pending_n >= _PENDING_CAP:
        raise ValueError("too many pending enrollments; ask the operator to "
                         "approve or reject some first")

    norm = {
        "id": eid,
        "url": url,
        "token": "",            # minted only on approval
        "enabled": False,       # activated on approval
        "status": STATUS_PENDING,
        "timeout": timeout,
    }
    items = [e for e in items if e["id"] != eid]
    items.append(norm)
    store.set_config(_REGISTRY_KEY, json.dumps(items))
    norm["default"] = False
    return norm


def approve(store, executor_id: str) -> Dict[str, Any]:
    """Approve a PENDING self-enrollment: mint its bearer token, flip to
    APPROVED + enabled. Returns the approved entry WITH the token under
    ``token`` (the API layer surfaces it to the operator ONCE so they can relay
    it to the executor out-of-band; it is never stored retrievably elsewhere
    and never returned again — only ``token_set`` is). Raises ``ValueError``
    if the id is the default, unknown, or already approved (use rotate_token to
    re-issue an approved executor's token)."""
    eid = (executor_id or "").strip()
    if not eid or eid == DEFAULT_ID:
        raise ValueError("the default executor is managed via /api/executor/config")
    items = _registry_raw(store)
    existing = next((e for e in items if e["id"] == eid), None)
    if existing is None:
        raise ValueError(f"no such pending executor: {eid!r}")
    if existing.get("status") == STATUS_APPROVED:
        raise ValueError(f"executor {eid!r} is already approved; "
                         "use rotate-token to re-issue")
    tok = _mint_token()
    norm = {
        "id": eid,
        "url": existing.get("url", ""),
        "token": tok,
        "enabled": True,
        "status": STATUS_APPROVED,
        "timeout": existing.get("timeout", 600),
    }
    items = [e for e in items if e["id"] != eid]
    items.append(norm)
    store.set_config(_REGISTRY_KEY, json.dumps(items))
    norm["default"] = False
    return norm


def rotate_token(store, executor_id: str) -> Dict[str, Any]:
    """Re-issue (rotate) an APPROVED named executor's token: the old token is
    invalidated, a new one is minted and returned ONCE (under ``token``). Use
    when the operator lost the one-time token shown at approval, or to rotate a
    suspected-leaked secret. Raises ``ValueError`` for the default, an unknown
    id, or a PENDING enrollment (approve it first)."""
    eid = (executor_id or "").strip()
    if not eid or eid == DEFAULT_ID:
        raise ValueError("the default executor is managed via /api/executor/config")
    items = _registry_raw(store)
    existing = next((e for e in items if e["id"] == eid), None)
    if existing is None:
        raise ValueError(f"no such executor: {eid!r}")
    if existing.get("status") == STATUS_PENDING:
        raise ValueError(f"executor {eid!r} is pending; approve it first")
    tok = _mint_token()
    norm = {
        "id": eid,
        "url": existing.get("url", ""),
        "token": tok,
        "enabled": existing.get("enabled", True),
        "status": STATUS_APPROVED,
        "timeout": existing.get("timeout", 600),
    }
    items = [e for e in items if e["id"] != eid]
    items.append(norm)
    store.set_config(_REGISTRY_KEY, json.dumps(items))
    norm["default"] = False
    return norm