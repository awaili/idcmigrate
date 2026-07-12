# data-gap — chunked plan (retroactive record)

The data-gap effort shipped as commit `9ac0b33`. This doc records the effort in
the chunked-protocol format (`docs/chunked-protocol.md`) — a worked example for
future efforts, and the durable record so a fresh session can see what was done
without replaying the chat.

## Goal

Model traditional-IDC information blindspots the F1-F11 fusion assumed away:
脱保 hardware, EOL OS/runtime, shadow IT, and hosts with no role/util/profile.
Across backend + CLI + web each.

## Blocks

- [x] **B1 hardware warranty/EOL** — `Server.warranty_status`+`hardware_eol`,
  `match.warranty_bucket`, F2 `eol_onprem_premium`, F10 `hw_support` signal,
  `ingest/warranty.py` fold-in + `fixtures/warranty.json`,
  `PUT /api/servers/{sid}/warranty`, CLI `idc warranty`, server-drawer badge.
  Acceptance: `test_warranty.py` (17). Commit: part of `9ac0b33`.
- [x] **B2 OS/runtime EOL** (the silent-rehost fix) — `core/eol.py`
  (`OS_EOL_TABLE`/`RUNTIME_EOL_TABLE` + `os_eol_bucket`/`runtime_eol_bucket` +
  `apply_os_eol`), wired in rebuild after `match_server` before `enrich_match`;
  F10 `os_support` signal. Acceptance: `test_eol.py` (9). Commit: part of `9ac0b33`.
- [x] **B3 data-gaps report + soft gate** — `core/datagaps.py`
  `portfolio_data_gaps`; `plan.plan_waves(min_assessment_confidence)` Needs-
  Discovery gate via `IDC_MIN_ASSESSMENT_CONFIDENCE`; `GET /api/data-gaps`;
  CLI `idc data-gaps`; web Data Quality tab. Acceptance: `test_datagaps.py` (5).
  Commit: part of `9ac0b33`.
- [x] **B4 shadow-IT discovery** — `ingest/discovery.py` `discover_drift`
  (unknown/orphan/drifted), `IDC_DISCOVERY_PATH` + `fixtures/discovery.json`,
  `discovery_snapshots` table + Store, `POST /api/discovery/scan` +
  `GET /api/discovery/diff`, CLI `idc discovery`, web Shadow IT card.
  Acceptance: `test_discovery.py` (8). Commit: part of `9ac0b33`.
- [x] **B5 UI pass** — `_server_out` precomputes `warranty_bucket`+
  `os_eol_bucket`; inventory Support column; drawer inline warranty form +
  OS-support row; readiness two-row grouped header; dashboard Data gaps card;
  graceful 404 on discovery card. Acceptance: full suite green. Commit: part of `9ac0b33`.

## Per-block think (retroactive)

### B1
- what: hardware support status field + derived bucket + cost/readiness/data-gaps wiring.
- why: 脱保 hardware is a migration trigger + risk the CMDB doesn't record.
- verify: `warranty_bucket` pure cases; eol premium 0 when onprem_rate=0 (F2
  tests use 0); hw_support n/a for unknown keeps existing rollups.

### B2
- what: detect EOL OS, dip match confidence + add replatform note/alternative.
- why: thai-idc-reality memory — silent rehost of CentOS 6 / Windows 2008 is a
  real migration failure.
- verify: `os_eol_bucket` table lookup; `apply_os_eol` no-op for active/unknown
  (so the 15K rebuild doesn't regress); live: 5591 hosts flagged.

### B3
- what: portfolio rollup of per-host F4 signals + a soft plan gate.
- why: confidence was per-host only; no portfolio view, no "don't plan thinly-
  known hosts" guard.
- verify: gate off by default (existing plan_waves behavior unchanged); None-
  confidence not gated; worst-hosts sort puts scored-low first.

### B4
- what: compare a network/vCenter snapshot vs CMDB.
- why: shadow IT is invisible to a ServiceNow-authoritative pipeline.
- verify: match by hostname/fqdn/ip; off when path unset; snapshot roundtrip.

### B5
- what: surface the blindspots across the actual browse surfaces.
- why: B1-B4 landed the signals but the UI only showed them in aggregate; the
  inventory list + dashboard + first-run 404 were weak.
- verify: `_server_out` additive (tests still pass); 404 -> friendly placeholder.

## Result

- Shipped: 4 new core modules (`eol`, `datagaps`, `ingest/warranty`,
  `ingest/discovery`) + 2 fixtures + Data Quality tab; F10 +2 signals.
- Suite: 321 passed, 2 skipped, 0 regressions (was 268; +53 tests).
- Live 15K smoke: warranty matched 6/7 (ghost-rack-07 -> shadow-IT), discovery
  3 unknown + 2 drifted, OS EOL flags 5591 hosts, readiness W1/W2 hw red.
- Config: `IDC_WARRANTY_PATH` / `IDC_DISCOVERY_PATH` / `IDC_MIN_ASSESSMENT_CONFIDENCE`
  all default OFF.
- Follow-ons (not done): dashboard could break gaps down by env/role; inventory
  facet filter on "OS EOL expired"; runtime EOL surfaced only in data-gaps
  count (not yet a readiness signal). Backlog, gated, or low-value.

## Note

This effort was actually done as a few large turns before the chunked protocol
was adopted — so the per-block commits are all folded into one `9ac0b33`.
Future efforts commit per block, as the protocol specifies.