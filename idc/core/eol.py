"""OS / runtime end-of-support detection (data-gap — traditional IDC).

The Thai-IDC reality ([[thai-idc-reality]]): CentOS 6/7, Windows Server
2008/2012, old Oracle Linux, JDK 7/8 are common and out of vendor support.
``match.match_server`` would otherwise silently rehost one to a CVM (a real
migration failure — the cloud has no supported image and the vendor won't
support a cloud deployment). This module classifies a server's OS (and, when a
code profile exists, its runtime) into the same support bucket as
``match.warranty_bucket`` so the match engine can flag replatform, the F10
readiness ``os_support`` signal can block cutover, and the data-gaps report can
count the blindspot.

Bundled static knowledge table (like ``match.REGION_MAP`` / ``CVM_TYPES`` — not
an ingest path, no off-by-default gate needed). Dates are public vendor EOL
dates; override per env by editing the table. Pure functions, no DB, no LLM.
"""
from __future__ import annotations

import datetime
from typing import Optional

from .models import Server

# (os, major-version-or-year) -> end-of-support ISO date. Keyed by the normalized
# OS name + the major version (Windows uses the release year). Unknown OS/
# versions fall through to "unknown" — no false positive.
OS_EOL_TABLE = {
    ("centos", "6"): "2020-11-30",
    ("centos", "7"): "2024-06-30",
    ("centos", "8"): "2021-12-31",
    ("centos", "stream8"): "2024-05-31",
    ("oracle linux", "6"): "2021-10-01",
    ("oracle linux", "7"): "2024-07-01",
    ("rhel", "6"): "2020-11-30",
    ("rhel", "7"): "2024-06-28",
    ("windows", "2008"): "2020-01-14",
    ("windows", "2008r2"): "2020-01-14",
    ("windows", "2012"): "2023-10-10",
    ("windows", "2012r2"): "2023-10-10",
    ("windows", "2016"): "2027-01-12",
    ("windows", "2019"): "2029-01-09",
    ("ubuntu", "14"): "2019-04-01",
    ("ubuntu", "16"): "2021-04-01",
    ("ubuntu", "18"): "2023-05-31",
    ("ubuntu", "20"): "2025-04-01",
    ("debian", "9"): "2022-06-30",
    ("debian", "10"): "2024-06-30",
    ("debian", "11"): "2026-08-15",
}

# runtime (from CodeProfile.runtime / language) -> EOL date. JDK LTS dates are
# the public Oracle / Adoptium support end-dates.
RUNTIME_EOL_TABLE = {
    ("jdk", "7"): "2022-07-19",
    ("jdk", "8"): "2030-12-31",   # LTS, long support but legacy
    ("jdk", "11"): "2026-09-30",
    ("jdk", "17"): "2029-09-30",
    ("openjdk", "7"): "2022-07-19",
    ("openjdk", "8"): "2030-12-31",
    ("openjdk", "11"): "2026-09-30",
    ("openjdk", "17"): "2029-09-30",
    ("python", "2"): "2020-01-01",
    ("python", "3.6"): "2021-12-23",
    ("python", "3.7"): "2023-06-27",
    ("node", "10"): "2021-04-30",
    ("node", "12"): "2022-04-30",
}

EXPIRED = "expired"
EXPIRING = "expiring"
ACTIVE = "active"
UNKNOWN = "unknown"
EXPIRING_DAYS = 90  # same window as hardware warranty


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _normalize_os(s: Server) -> tuple:
    """(os-lower, major-version-token) for table lookup, e.g. ('centos','7').

    Normalizes both halves: the OS name strips the redundant "server" token
    (``windows server`` -> ``windows``) and the version collapses to a major
    token (``2012 r2`` -> ``2012r2``, ``7.9`` -> ``7``).
    """
    os_name = (s.os or "").strip().lower().replace(" server", "").strip()
    ver = (s.os_version or "").strip().lower()
    ver = ver.replace("server", "").strip()
    ver = ver.replace(" ", "")            # "2008 r2" -> "2008r2"
    if not ver and os_name:
        # no version -> can't look up; return os-only for callers to treat unknown
        return os_name, ""
    major = ver.split(".")[0] if "." in ver else ver
    return os_name, (major or "")


def os_eol_date(s: Server) -> str:
    """The EOL ISO date for the server's OS, or "" when unknown/unmatched."""
    os_name, ver = _normalize_os(s)
    if not os_name or not ver:
        return ""
    # try (os, ver) then (os, ver without trailing r2/stream) fuzzy fallbacks
    for key in ((os_name, ver), (os_name, ver.replace("r2", "").replace("stream", ""))):
        if key in OS_EOL_TABLE:
            return OS_EOL_TABLE[key]
    return ""


def os_eol_bucket(s: Server, today_iso: Optional[str] = None) -> str:
    """OS support bucket: expired / expiring / active / unknown.

    Unlike ``warranty_bucket`` there's no operator override — the bucket is
    always derived from the OS version vs the EOL table. ``unknown`` when the
    OS/version isn't in the table (we don't flag what we don't know).
    """
    eol = os_eol_date(s)
    if not eol:
        return UNKNOWN
    today = today_iso or _today_iso()
    try:
        eol_d = datetime.date.fromisoformat(eol)
        today_d = datetime.date.fromisoformat(today)
    except ValueError:
        return UNKNOWN
    if eol_d < today_d:
        return EXPIRED
    if (eol_d - today_d).days <= EXPIRING_DAYS:
        return EXPIRING
    return ACTIVE


def runtime_eol_bucket(runtime: str, language: str = "",
                        today_iso: Optional[str] = None) -> str:
    """Runtime support bucket from a CodeProfile.runtime/language string."""
    rt = (runtime or language or "").strip().lower()
    if not rt:
        return UNKNOWN
    # parse family + version, e.g. "jdk8" -> ("jdk","8"), "openjdk-11" -> ("openjdk","11")
    fam = ver = ""
    for prefix in ("openjdk", "jdk", "python", "node", "ruby", "php", "go"):
        if rt.startswith(prefix):
            fam = prefix
            ver = rt[len(prefix):].lstrip("-").lstrip(".").strip()
            break
    if not fam:
        return UNKNOWN
    # collapse "3.6" -> "3.6" for python (we key 3.6/3.7), "8" -> "8" for jdk
    if fam == "python" and ver:
        ver = ver if "." in ver else ver
    if not ver:
        return UNKNOWN
    for key in ((fam, ver), (fam, ver.split(".")[0])):
        if key in RUNTIME_EOL_TABLE:
            eol = RUNTIME_EOL_TABLE[key]
            break
    else:
        return UNKNOWN
    today = today_iso or _today_iso()
    try:
        eol_d = datetime.date.fromisoformat(eol)
        today_d = datetime.date.fromisoformat(today)
    except ValueError:
        return UNKNOWN
    if eol_d < today_d:
        return EXPIRED
    if (eol_d - today_d).days <= EXPIRING_DAYS:
        return EXPIRING
    return ACTIVE


__all__ = ["OS_EOL_TABLE", "RUNTIME_EOL_TABLE",
           "os_eol_bucket", "os_eol_date", "runtime_eol_bucket",
           "apply_os_eol",
           "EXPIRED", "EXPIRING", "ACTIVE", "UNKNOWN", "EXPIRING_DAYS"]


# confidence dip on the base rule confidence for an EOL OS — a thinly-fit rule,
# not a clean rehost. Compounds with the F4 coverage scale in enrich_match.
_EOL_DIP = {EXPIRED: 0.75, EXPIRING: 0.92, ACTIVE: 1.0, UNKNOWN: 1.0}
_EOL_NOTE = {
    EXPIRED: "OS out of vendor support — replatform to a supported base image, "
             "do not lift-and-shift the EOL image",
    EXPIRING: "OS nearing end-of-support — plan a supported-base-image replatform",
}


def apply_os_eol(match, server: Server, today_iso: Optional[str] = None):
    """Flag a match whose server OS is out of support (the silent-rehost fix).

    Mutates ``match`` in place: dips the base rule confidence, annotates the
    rationale with the EOL status + replatform guidance, and appends a
    "replatform to supported base image" alternative. No-op when the OS is
    active/unknown (we don't flag what we don't know). Called by ``rebuild``
    after ``match_server`` and before ``enrich_match`` so the EOL dip compounds
    with the F4 coverage scale.
    """
    bucket = os_eol_bucket(server, today_iso)
    if bucket not in (EXPIRED, EXPIRING):
        return match
    dip = _EOL_DIP.get(bucket, 1.0)
    match.confidence = max(0.05, (match.confidence or 0) * dip)
    note = _EOL_NOTE[bucket]
    match.rationale = (match.rationale or "") + f" [OS {bucket} — {note}]"
    alts = list(match.alternatives or [])
    replat = "replatform to a supported base image (e.g. TencentOS / Ubuntu)"
    if replat not in alts:
        alts.append(replat)
    match.alternatives = alts
    return match