"""F2 — TCO / business case + tag-driven retain/retire.

Pure functions (no DB) that turn matched targets + Tencent Cloud pricing into
per-server run-cost estimates and a portfolio business case. The store
persists one ``cost_estimates`` row per server (``Store.upsert_cost``) and
immutable business-case snapshots (``Store.save_business_case``); ``Match.cost``
carries a hydrated copy for the UI / agent.

Pricing source (``IDC_PRICING_URL``): a live Tencent Cloud pricing endpoint if
configured, else the bundled ``fixtures/pricing.json`` so the business case
renders out-of-the-box (same fallback pattern as the inventory adapters).
``IDC_PRICING_OVERRIDE_PATH`` is an optional JSON override map for customer
contract pricing (same ``old -> new`` shape as the executor overrides) — when
present, ``pricing_source`` flips to ``contract``.

Tag-driven retain/retire (à la Azure ``AzM.MigrationIntent``): a server tagged
``idc:retain`` / ``idc:retire`` yields an operator-sourced ``AppStrategy`` for
each of its apps, which the wave planner honors WITHOUT an LLM call (the
strategies overlay excludes retain/retire apps from migration waves).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import FIXTURES, ROOT, Settings
from .models import (
    AppStrategy, CostEstimate, Match, Server,
    PATTERN_RETAIN, PATTERN_RETIRE, STRATEGY_SOURCE_OPERATOR, _now,
)


# ---------------------------------------------------------------------------
# price book
# ---------------------------------------------------------------------------
@dataclass
class PriceBook:
    """Tencent Cloud pricing, two-tier lookup.

    ``by_spec[product][spec_key][region] = {monthly, yearly}`` gives per-SKU
    precision (populated by the live pricing API or the detailed fixture).
    ``by_product[product][region]`` is the per-product default used when the
    spec isn't priced individually. ``onprem_rate`` is a flat USD/month per
    server for the IDC current-cost side of the TCO (0 = unknown -> savings
    can't be computed, but the cloud run cost still renders).
    """
    by_spec: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = field(default_factory=dict)
    by_product: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    source: str = "fixture"        # fixture / public / contract
    onprem_rate: float = 0.0
    loaded_at: str = field(default_factory=_now)


_PRICEBOOK_CACHE: Optional[Tuple[PriceBook, str]] = None  # (book, source-url-or-path)


def _read_fixture(path: str) -> Dict[str, Any]:
    p = Path(path) or (FIXTURES / "pricing.json")
    if not p.exists():
        # FIXTURES may point at a scale fixture dir (IDC_FIXTURES_DIR) that
        # ships no pricing.json — fall back to the BUNDLED fixtures/pricing.json
        # (ROOT/fixtures), not FIXTURES itself (which is the same missing file).
        p = ROOT / "fixtures" / "pricing.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_live(raw: Any) -> Dict[str, Any]:
    """Parse a live pricing API response into the by_spec/by_product shape.

    The expected shape matches the fixture (``by_spec``/``by_product``); a
    real integration would map the Tencent Cloud Pricing API response into
    this shape. Tolerant: if it doesn't parse, returns {} so the caller falls
    back to the fixture.
    """
    if isinstance(raw, dict) and ("by_spec" in raw or "by_product" in raw):
        return raw
    return {}


def _apply_overrides(book: PriceBook, override_path: str) -> None:
    """Apply a JSON override map (contract pricing) onto the price book.

    Keys (any of):
      * ``"{product}:{spec_key}:{region}"`` -> ``{"monthly": x, "yearly": y}``
      * ``"onprem_rate"`` -> number (flat USD/month per server)
    Missing yearly defaults to monthly*12. Sets ``source='contract'``.
    """
    if not override_path:
        return
    p = Path(override_path)
    if not p.exists():
        return
    try:
        ov = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(ov, dict):
        return
    if isinstance(ov.get("onprem_rate"), (int, float)):
        book.onprem_rate = float(ov["onprem_rate"])
    for k, v in ov.items():
        if k == "onprem_rate" or not isinstance(v, dict):
            continue
        parts = k.split(":")
        if len(parts) != 3:
            continue
        product, spec_key, region = parts
        cell = book.by_spec.setdefault(product, {}).setdefault(spec_key, {}).setdefault(region, {})
        if isinstance(v.get("monthly"), (int, float)):
            cell["monthly"] = float(v["monthly"])
        if isinstance(v.get("yearly"), (int, float)):
            cell["yearly"] = float(v["yearly"])
        elif "monthly" in cell:
            cell["yearly"] = cell["monthly"] * 12
        book.source = "contract"


def load_pricebook(settings: Settings, refresh: bool = False) -> PriceBook:
    """Load + cache the price book. Live API if ``IDC_PRICING_URL`` set, else
    the bundled fixture; overrides applied on top. Falls back to fixture on
    any live-parsing failure so the business case always renders."""
    global _PRICEBOOK_CACHE
    cache_key = f"{settings.pricing_url}|{settings.pricing_override_path}"
    if _PRICEBOOK_CACHE and not refresh and _PRICEBOOK_CACHE[1] == cache_key:
        return _PRICEBOOK_CACHE[0]

    raw: Dict[str, Any] = {}
    source = "fixture"
    if settings.has_pricing():
        try:
            import httpx
            r = httpx.get(settings.pricing_url, timeout=20)
            if r.status_code == 200:
                parsed = _parse_live(r.json())
                if parsed:
                    raw = parsed
                    source = "public"
        except Exception:
            pass
    if not raw:
        raw = _read_fixture(str(FIXTURES / "pricing.json"))
        source = "fixture"

    book = PriceBook(
        by_spec=raw.get("by_spec") or {},
        by_product=raw.get("by_product") or {},
        source=source,
        onprem_rate=float(raw.get("onprem_rate_default") or 0.0),
        loaded_at=_now(),
    )
    _apply_overrides(book, settings.pricing_override_path)
    _PRICEBOOK_CACHE = (book, cache_key)
    return book


# ---------------------------------------------------------------------------
# per-server estimate
# ---------------------------------------------------------------------------
_CDB_SPEC_RE = re.compile(r"(MYSQL\.HighMem\s*-\s*\d+GB)")


def _spec_price_key(product: str, spec: str) -> str:
    """Reduce a Match.target.spec to a price-book key."""
    if not spec:
        return "_default"
    if product == "CDB":
        m = _CDB_SPEC_RE.search(spec)
        return m.group(1) if m else spec
    if product == "TKE":
        return "_default"   # managed control plane has no per-master VM cost
    return spec


def _lookup(book: PriceBook, product: str, spec_key: str,
            region: str) -> Tuple[float, float]:
    """(monthly, yearly) for product+spec+region; falls back to product default
    then 0/0 when unpriced."""
    cell = ((book.by_spec.get(product) or {}).get(spec_key) or {}).get(region)
    if not cell:
        cell = ((book.by_spec.get(product) or {}).get(spec_key) or {}).get("_default")
    if not cell:
        cell = (book.by_product.get(product) or {}).get(region)
    if not cell:
        cell = (book.by_product.get(product) or {}).get("_default")
    if not cell:
        return 0.0, 0.0
    monthly = float(cell.get("monthly") or 0.0)
    yearly = float(cell.get("yearly") or (monthly * 12))
    return monthly, yearly


def estimate_server(server: Server, match: Match, book: PriceBook) -> CostEstimate:
    """Per-server run-cost estimate from the matched target + price book."""
    product = (match.target.product or "") if match and match.target else ""
    spec = (match.target.spec or "") if match and match.target else ""
    region = (match.target.region or "") if match and match.target else ""
    spec_key = _spec_price_key(product, spec)
    monthly, yearly = _lookup(book, product, spec_key, region)
    return CostEstimate(
        server_id=server.id, target_product=product, spec=spec, region=region,
        monthly_usd=monthly, yearly_usd=yearly,
        basis=server.sizing_basis or "estimated",
        pricing_source=book.source, computed_at=_now(),
    )


# ---------------------------------------------------------------------------
# F2 + data-gap — on-prem extended-support premium for out-of-warranty hardware
# ---------------------------------------------------------------------------
# Traditional-IDC reality: a 脱保 / out-of-warranty host still running on-prem
# often carries an extended-support or refresh-risk cost (vendor premium, parts
# scarcity, security-patch gap) that the flat ``onprem_rate`` doesn't capture.
# This adds a per-host premium on the IDC current-cost side of the TCO so the
# business case reflects the real cost of *not* migrating EOL hardware. The
# premium is a fraction of ``onprem_rate`` by warranty bucket; it is 0 when
# ``onprem_rate`` is 0 (savings unknown) so the out-of-the-box fixture path and
# the F2 tests (which set no onprem_rate) are unaffected.
EOL_ONPREM_PREMIUM = {
    "expired": 0.50,   # out of warranty — extended support / refresh risk
    "expiring": 0.20,  # within 90d of end-of-support
    "active": 0.0,
    "unknown": 0.0,    # unmodeled support status is neutral on cost (data-gaps surfaces it)
}


def eol_onprem_premium_monthly(server: Server, book: PriceBook,
                                today_iso: Optional[str] = None) -> float:
    """Extra USD/month on the on-prem side for an out-of-warranty host.

    0 when ``book.onprem_rate`` is 0 (savings can't be computed) or the host is
    under warranty / unassessed. Uses ``match.warranty_bucket`` to classify."""
    if not book.onprem_rate:
        return 0.0
    from .match import warranty_bucket
    bucket = warranty_bucket(server, today_iso)
    return book.onprem_rate * EOL_ONPREM_PREMIUM.get(bucket, 0.0)


# ---------------------------------------------------------------------------
# portfolio business case
# ---------------------------------------------------------------------------
def _strategy_for(server: Server, strategies: Dict[str, AppStrategy],
                  profiles: Optional[Dict[str, Any]] = None) -> str:
    """6R strategy for a server's app (first app with a strategy), else rehost."""
    for a in server.app_ids:
        if a in strategies:
            return strategies[a].strategy
    return "rehost"


def estimate_portfolio(servers: List[Server], matches: List[Match],
                       book: PriceBook,
                       strategies: Optional[Dict[str, AppStrategy]] = None
                       ) -> Dict[str, Any]:
    """Portfolio TCO + per-strategy rollup (F2).

    Returns a JSON-serializable dict with cloud monthly/yearly, IDC current
    cost (if ``onprem_rate`` > 0), annual savings, and per-server / per-product
    / per-region / per-strategy breakdowns.
    """
    strategies = strategies or {}
    match_by_id = {m.server_id: m for m in matches}
    per_server: List[Dict[str, Any]] = []
    cloud_monthly = cloud_yearly = 0.0
    onprem_monthly = onprem_yearly = 0.0
    eol_premium_yearly = 0.0
    per_product: Dict[str, float] = {}
    per_region: Dict[str, float] = {}
    per_strategy: Dict[str, Dict[str, float]] = {}
    priced = unpriced = 0

    for s in servers:
        m = match_by_id.get(s.id)
        if not m:
            continue
        est = estimate_server(s, m, book)
        cloud_monthly += est.monthly_usd
        cloud_yearly += est.yearly_usd
        if book.onprem_rate > 0:
            onprem_monthly += book.onprem_rate
            onprem_yearly += book.onprem_rate * 12
            # F2 + data-gap — extended-support premium for out-of-warranty hosts
            prem = eol_onprem_premium_monthly(s, book)
            if prem:
                onprem_monthly += prem
                onprem_yearly += prem * 12
                eol_premium_yearly += prem * 12
        strat = _strategy_for(s, strategies)
        # non-migrating strategies contribute 0 cloud cost (host stays/retires)
        if strat in (PATTERN_RETAIN, PATTERN_RETIRE):
            cloud_monthly -= est.monthly_usd
            cloud_yearly -= est.yearly_usd
        per_product[est.target_product] = per_product.get(est.target_product, 0.0) + est.yearly_usd
        per_region[est.region] = per_region.get(est.region, 0.0) + est.yearly_usd
        bucket = per_strategy.setdefault(strat, {"servers": 0, "yearly": 0.0})
        bucket["servers"] += 1
        bucket["yearly"] += est.yearly_usd if strat not in (PATTERN_RETAIN, PATTERN_RETIRE) else 0.0
        per_server.append({
            "server_id": s.id, "hostname": s.hostname, "app_ids": list(s.app_ids),
            "product": est.target_product, "spec": est.spec, "region": est.region,
            "monthly": round(est.monthly_usd, 2), "yearly": round(est.yearly_usd, 2),
            "basis": est.basis, "strategy": strat,
            "eol_premium_yearly": round(eol_onprem_premium_monthly(s, book) * 12, 2),
        })
        if est.monthly_usd > 0:
            priced += 1
        else:
            unpriced += 1

    annual_savings = (onprem_yearly - cloud_yearly) if book.onprem_rate > 0 else None
    return {
        "currency": "USD",
        "pricing_source": book.source,
        "generated_at": _now(),
        "cloud_monthly": round(cloud_monthly, 2),
        "cloud_yearly": round(cloud_yearly, 2),
        "onprem_monthly": round(onprem_monthly, 2) if book.onprem_rate > 0 else 0.0,
        "onprem_yearly": round(onprem_yearly, 2) if book.onprem_rate > 0 else 0.0,
        "eol_premium_yearly": round(eol_premium_yearly, 2),
        "annual_savings": round(annual_savings, 2) if annual_savings is not None else None,
        "priced_servers": priced,
        "unpriced_servers": unpriced,
        "per_product": {k: round(v, 2) for k, v in per_product.items()},
        "per_region": {k: round(v, 2) for k, v in per_region.items()},
        "per_strategy": {k: {"servers": v["servers"], "yearly": round(v["yearly"], 2)}
                         for k, v in per_strategy.items()},
        "per_server": per_server,
    }


def what_if(servers: List[Server], matches: List[Match], book: PriceBook,
            region: Optional[str] = None,
            sizing: Optional[str] = None,
            byol: Optional[bool] = None,
            strategies: Optional[Dict[str, AppStrategy]] = None) -> Dict[str, Any]:
    """Re-estimate the portfolio under alternate assumptions (F3 driver).

    Supports three independent overrides (composable), applied without
    re-running ingest:
      * ``region``  — re-price every match in this region (re_region).
      * ``sizing``  — re-size every server under this strategy
                      (``"as_is"`` / ``"measured"`` / ``"right_size"``); uses
                      ``match.right_size`` so the spec actually shrinks when
                      utilization is low. ``None`` keeps the persisted match.
      * ``byol``    — hint flag (currently informational; pricing already
                      reflects whatever license tag the match carries). Reserved
                      for the F3 BYOL-vs-included lever.

    Returns baseline + alternate cloud yearly + the delta. Pure w.r.t. the
    inputs (loads no DB); the caller gathers persisted servers/matches.
    """
    import dataclasses
    from . import match as match_mod
    baseline = estimate_portfolio(servers, matches, book, strategies)
    applied: Dict[str, Any] = {"region": region, "sizing": sizing, "byol": byol}
    if not region and not sizing and byol is None:
        return {"baseline": baseline, "alternate": None, "delta": None,
                "applied": applied}

    s_by_id = {s.id: s for s in servers}
    alt_matches: List[Match] = []
    for m in matches:
        s = s_by_id.get(m.server_id)
        nm = m
        # sizing override re-runs the rule engine (needs the Server)
        if sizing and s is not None:
            nm = match_mod.right_size(s, strategy=sizing)
        # region override swaps region/az on whatever target we now have
        if region:
            new_target = dataclasses.replace(nm.target, region=region,
                                             az=match_mod._az(region))
            nm = dataclasses.replace(nm, target=new_target)
        alt_matches.append(nm)
    alternate = estimate_portfolio(servers, alt_matches, book, strategies)
    delta = round(alternate["cloud_yearly"] - baseline["cloud_yearly"], 2)
    return {"baseline": baseline, "alternate": alternate, "delta": delta,
            "applied": applied}


# ---------------------------------------------------------------------------
# tag-driven retain / retire (operator override, no LLM)
# ---------------------------------------------------------------------------
TAG_RETAIN = "idc:retain"
TAG_RETIRE = "idc:retire"


def strategies_from_tags(servers: List[Server]) -> Dict[str, AppStrategy]:
    """Derive operator-sourced retain/retire AppStrategies from server tags.

    A server carrying ``idc:retain`` / ``idc:retire`` marks all its apps with
    the corresponding 7R strategy (``source='operator'``), so the wave planner
    excludes those apps from migration waves WITHOUT an LLM call — the
    operator's explicit intent wins. If both tags appear on one server,
    ``retain`` wins (the safer non-migration outcome).
    """
    out: Dict[str, AppStrategy] = {}
    for s in servers:
        tags = {t.lower().strip() for t in (s.tags or [])}
        strat = None
        rationale = ""
        if TAG_RETAIN in tags:
            strat, rationale = PATTERN_RETAIN, f"operator tag {TAG_RETAIN} on {s.hostname}"
        elif TAG_RETIRE in tags:
            strat, rationale = PATTERN_RETIRE, f"operator tag {TAG_RETIRE} on {s.hostname}"
        if not strat:
            continue
        for app_id in s.app_ids:
            # don't clobber an existing retain (safer) with a retire from another host
            existing = out.get(app_id)
            if existing and existing.strategy == PATTERN_RETAIN and strat == PATTERN_RETIRE:
                continue
            out[app_id] = AppStrategy(
                app_id=app_id, strategy=strat, rationale=rationale,
                target="on-prem" if strat == PATTERN_RETAIN else "decommission",
                confidence=1.0, source=STRATEGY_SOURCE_OPERATOR,
                assigned_at=_now(), updated_at=_now(),
            )
    return out