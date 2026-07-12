# gap-actionable — chunked plan

## Goal

The data-gap buckets (warranty, OS-EOL) are now visible as badges in the
inventory list (B5 of `9ac0b33`) but you can't filter to them. Make them
facetable + quick-filterable so an operator can click "OS EOL expired" and land
on the 5591 hosts that need a replatform. Across backend + CLI + web.

## Why this

B5 answered "can we see the blindspot while browsing?" This answers "can we act
on it?" — the inventory is the primary triage surface; a one-click filter to the
脱保 / EOL-OS cohort is the thing an operator actually wants the day after the
report shows 5591 expired-OS hosts.

## Blocks

- [x] **B1 persist derived buckets** — add `warranty_bucket` + `os_eol_bucket`
  as stored server columns (db schema + ALTER + `_SERVER_COLS` + upsert +
  `_row_to_server`); compute + set them in `rebuild` (after the warranty merge,
  alongside the F4 loop); recompute `warranty_bucket` in
  `PUT /api/servers/{sid}/warranty`. `_server_out` reads the persisted bucket
  (fall back to compute if stale/NULL). Acceptance: rebuild persists non-NULL
  buckets; a server's `os_eol_bucket` survives a fresh load. Commit: B1 below.
- [x] **B2 inventory facets + quick filters** — `Store._facets` groups by the
  two new columns; `ServerFilter` gains `warranty_bucket` + `os_eol_bucket`
  params + `_filters_sql` clauses; `GET /api/servers` accepts them;
  inventory.js renders facet chips for the buckets + quick-apply buttons
  "EOL OS expired" / "脱保 (out of warranty)". Acceptance: clicking the facet
  or quick-filter narrows the list to that cohort; backend test for the filter.
  Commit: B2 below.
- [x] **B3 combine** — full `pytest`; update this doc's Result; wrap commit.
  Acceptance: 321+ green, no regressions. Commit: B3 below.

## Per-block think (filled as we go)

### B1
- what: denormalize the two derived buckets into stored columns so SQL can
  facet/filter them (the buckets are currently computed in Python at read time).
- why: a 15K GROUP BY / WHERE on a derived value needs it stored; computing
  facets in Python after fetch doesn't scale.
- verify: staleness — `warranty_bucket` must recompute on the PUT-warranty path;
  `os_eol_bucket` only changes on re-ingest (re-runs rebuild), so no extra hook.

### B2
- what: expose the buckets as inventory facets + one-click quick filters.
- why: that's the actionable surface — "show me the bad cohort".
- verify: facet counts match the data-gaps report totals; filter round-trips.

### B3
- what: full-suite + record.
- why: B1 touches the rebuild path + a schema ALTER (cross-cutting); needs the
  whole suite, not just block-local tests.
- verify: 321+ passed, 0 regressions; live: inventory facet for expired OS
  shows ~5591.

## Result

- Shipped in 3 block-commits: B1 `b7fd4c6` (persist derived buckets), B2
  `cc99ab0` (inventory facets + quick filters), B3 this commit (combine).
- Suite: 323 passed, 2 skipped, 0 regressions (was 321; +2 B2 tests).
- Live smoke (15-server fixture after the suite shrank the shared DB):
  `os_eol_bucket` facet `{expired:10, unknown:5}`, `warranty_bucket`
  `{unknown:15}`, `os_eol_bucket=expired` filter narrows to the 10-host cohort.
  On the 15K scale estate the expired-OS cohort was 5591 (data-gaps report) —
  same filter/facet, same behavior.
- Operator UX: one click on "⌖ EOL OS (expired)" or the `OS EOL → expired`
  facet chip lands on the replatform cohort; "⌖ 脱保" lands on out-of-warranty.
- Staleness handled: PUT /api/servers/{sid}/warranty recomputes warranty_bucket;
  os_eol_bucket recomputes on rebuild (re-ingest). _server_out falls back to
  computing for rows that predate the column.
- Follow-on (not done, low value): the buckets could also drive a wave-plan
  bias (expired-OS hosts replatform-first) — left for a later effort.