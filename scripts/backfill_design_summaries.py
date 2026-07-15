"""Backfill the AI ``summary`` column for saved LZ + network designs that
predate the summary feature (the "AI description" column on the Saved designs /
Saved network designs tables).

The AI description already exists in each design's own data — MigraQ authored it
as ``requirements.notes`` (LZ designs) / ``policy.notes`` (network designs) when
the design was generated. The ``summary`` column was added later, so older rows
have it empty. This script promotes that AI-authored text into ``summary`` so
the column is populated without re-running the LLM.

Idempotent: only fills an empty ``summary``; never overwrites a non-empty one.
Data-only — no schema change, no backend restart required for the data itself
(the running backend must already serve the summary field for the UI to show
it; restart idc-migrate if it predates the summary endpoints).

Run against the live DB:
  export IDC_DB_URL='mysql://idc_migrate_app:IdcMigrate%232026%21@10.0.0.3:3306/idc_migrate'
  PYTHONPATH=. python scripts/backfill_design_summaries.py
"""
import os
import sys

from idc.core.db import open_store
from idc.config import get_settings


def _clip(s: str) -> str:
    return (s or "").strip()[:500]


def main() -> int:
    st = open_store(os.environ.get("IDC_DB_URL") or get_settings().db_url)
    changed = []
    # LZ designs: summary <- requirements.notes (AI-authored)
    for d in st.list_lz_designs():
        if (d.summary or "").strip():
            continue
        notes = _clip((d.requirements or {}).get("notes"))
        if not notes:
            continue
        d.summary = notes
        st.upsert_lz_design(d)
        changed.append(("LZ", d.design_id, d.name, notes))
    # Network designs: summary <- policy.notes (AI-authored carving description)
    for nd in st.list_network_designs():
        if (nd.summary or "").strip():
            continue
        notes = _clip((nd.policy or {}).get("notes"))
        if not notes:
            continue
        nd.summary = notes
        st.upsert_network_design(nd)
        changed.append(("NET", nd.design_id, nd.name, notes))
    st.close()

    print(f"Backfilled {len(changed)} design summary(ies):")
    for kind, did, name, s in changed:
        print(f"  [{kind}] {did} | {name}")
        print(f"         -> {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())