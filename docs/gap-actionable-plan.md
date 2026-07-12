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

- [ ] **B1 persist derived buckets** — add `warranty_bucket` + `os_eol_bucket`
  as stored server columns (db schema + ALTER + `_SERVER_COLS` + upsert +
  `_row_to_server`); compute + set them in `rebuild` (after the warranty merge,
  alongside the F4 loop); recompute `warranty_bucket` in
  `PUT /api/servers/{sid}/warranty`. `_server_out` reads the persisted bucket
  (fall back to compute if stale/NULL). Acceptance: rebuild persists non-NULL
  buckets; a server's `os_eol_bucket` survives a fresh load. Commit: <sha>.
- [ ] **B2 inventory facets + quick filters** — `Store._facets` groups by the
  two new columns; `ServerFilter` gains `warranty_bucket` + `os_eol_bucket`
  params + `_filters_sql` clauses; `GET /api/servers` accepts them;
  inventory.js renders facet chips for the buckets + quick-apply buttons
  "EOL OS expired" / "脱保 (expired/expiring)". Acceptance: clicking the facet
  or quick-filter narrows the list to that cohort; backend test for the filter.
  Commit: <sha>.
- [ ] **B3 combine** — full `pytest`; update this doc's Result; wrap commit.
  Acceptance: 321+ green, no regressions. Commit: <sha>.

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

<filled at combine time>