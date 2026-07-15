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
_TRUTHY = ("1", "true", "yes", "on")
_LEGACY_KEYS = ("executor_url", "executor_token", "executor_enabled",
                "executor_timeout")


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


def resolve(store, settings: Settings, executor_id: Optional[str]) -> Settings:
    """Return a Settings copy overlaid with the selected executor's
    url/token/enabled/timeout + the global ``public_url``. ``executor_id``
    None/''/'default' → the default executor. Raises ``KeyError`` if the named
    executor isn't registered (caller maps that to a 404)."""
    regs = list_executors(store, settings)
    eid = (executor_id or "").strip()
    if not eid or eid == DEFAULT_ID:
        chosen = regs[0]
    else:
        chosen = next((e for e in regs if e["id"] == eid), None)
        if chosen is None:
            raise KeyError(eid)
    return dataclasses.replace(
        settings,
        executor_url=chosen["url"],
        executor_token=chosen["token"],
        executor_enabled=chosen["enabled"],
        executor_timeout=chosen["timeout"],
        public_url=_global_public_url(store, settings),
    )


def valid_tokens(store, settings: Settings) -> List[str]:
    """Every bearer token accepted on the push direction (any registered
    executor may push back to idc-migrate; the default's token is included)."""
    return [e["token"] for e in list_executors(store, settings) if e.get("token")]


def resolve_by_token(store, settings: Settings, token: str) -> Optional[str]:
    """Map a bearer token to its executor id (constant-time compare). Returns
    the executor id ("default" for the default executor) or None when no
    registered executor's token matches. Used by the pull-dispatch claim
    endpoint to identify which executor is claiming (so a task targeted at a
    specific executor is only claimable by that executor's token)."""
    if not token:
        return None
    for e in list_executors(store, settings):
        et = e.get("token") or ""
        if et and secrets.compare_digest(token, et):
            return e["id"]
    return None


def public_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the token (keep only ``token_set``) for API/UI consumption."""
    out = dict(entry)
    out["token_set"] = bool(out.pop("token", ""))
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
        "timeout": timeout,
    }
    items = _registry_raw(store)
    items = [e for e in items if e["id"] != eid]
    items.append(norm)
    store.set_config(_REGISTRY_KEY, json.dumps(items))
    norm["default"] = False
    return norm


def delete(store, executor_id: str) -> bool:
    """Remove a NAMED executor. Returns False if it wasn't there; raises
    ``ValueError`` for the default (can't delete it)."""
    eid = (executor_id or "").strip()
    if not eid or eid == DEFAULT_ID:
        raise ValueError("cannot delete the default executor")
    items = _registry_raw(store)
    new = [e for e in items if e["id"] != eid]
    if len(new) == len(items):
        return False
    store.set_config(_REGISTRY_KEY, json.dumps(new))
    return True