# Chunked working protocol

This project can produce large multi-file changes that blow up context and API
response size. To keep each turn small and resumable, we work in **checkpointed
blocks**. This doc is the operating rules — read it in any (even trimmed)
session before starting an effort.

## Rules

1. **Plan first, on disk.** Before touching code, write
   `docs/<effort>-plan.md` (copy the template at the bottom of this file):
   the block list, each block's scope + acceptance check, and a checklist.
   This file is the durable thread — it survives context summarization, so a
   fresh/trimmed session resumes from the file, not the chat history.

2. **One block per turn.** A turn does ONE block only:
   - short **think note** (≤5 lines): what / why / how I'll verify — the exposed
     reasoning.
   - small focused diff (one subsystem where possible).
   - run the relevant tests (serial — shared mutable DB, see memory).
   - `git commit` = the checkpoint (progress is durable even if context wipes).
   - one-line status + tick the checklist box in the plan doc.

3. **No mega-turns.** If a block would touch >~4 files or two subsystems, split
   it. Smaller prompt in → smaller response out.

4. **Combine at the end.** When every box is ticked, one final turn does the
   cross-block integration pass: full `pytest`, update the plan doc's "Result"
   section, optional docs sync, wrap commit. The checklist is the merge record.

5. **Task list mirrors blocks** (`TaskCreate`/`TaskList`) so progress is
   visible without reading diffs.

## On "expose the think procedure"

The per-block think note IS the exposed reasoning. The literal chain-of-thought
display is a client/session setting (extended-thinking visibility), not
toggleable from inside a turn — if the client has it, enable it; the prose
think-note is the in-response equivalent.

## Template — `docs/<effort>-plan.md`

```
# <effort> — chunked plan

## Goal
<one or two lines: what + why>

## Blocks
- [ ] B1 <name> — <scope>. Acceptance: <test/check>. Commit: <sha>
- [ ] B2 <name> — <scope>. Acceptance: <test/check>. Commit: <sha>
...

## Per-block think (filled as we go)
### B1
- what:
- why:
- verify:

## Result
<filled at the end: what shipped, suite status, live smoke, follow-ons>
```

## Worked example

`docs/data-gap-plan.md` is this protocol applied retroactively to the data-gap
effort (commit `9ac0b33`) — use it as the format reference.