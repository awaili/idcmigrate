"""Skill runner — dispatches a loaded skill to its backend (chat or codex),
parses the output, and runs the propose→validate→loop when the skill declares
a validator + ``max_rounds > 1``.

This unifies the two agent paths under one abstraction:
  * ``backend: chat``  — ``LLMClient.chat`` (single-shot). The skill's body is
    the system message; ``user_content`` is the user message. ``output: json``
    pulls the first balanced object out of the reply via ``_extract_json``;
    ``output: text`` returns the raw reply. A validator + ``max_rounds>1`` loops
    on parse failure / validator errors (same shape as ``design_lz``).
  * ``backend: codex`` — the codex agent harness via ``run_agent`` (the
    ``codex_runner`` / ``claude_runner`` dispatcher). The skill's body is fed
    as the agent context (its system prompt) and ``user_content`` as the task;
    ``mode`` selects read-only vs workspace-write. Lets a skill use codex's
    tool-use / file-read / shell when the task needs it (e.g. a code-grounded
    audit skill), while 7R / waveplan stay on the cheap chat path.

Imports of ``LLMClient`` / ``_extract_json`` / ``run_agent`` are done lazily
inside ``run_skill`` to avoid a circular import: ``client.py`` imports this
module (so its LLM-call methods can call ``run_skill``), and this module needs
``client``'s ``LLMClient`` + ``_extract_json``. Lazy function-level imports
break the cycle cleanly.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Dict, Optional, Tuple

from ..config import Settings, get_settings
from .skills import Skill, get_skill

# validator registry — a validator checks + may post-process the parsed JSON
# object. Signature: (obj, ctx) -> (ok, errors, obj). ``ok``=False + non-empty
# errors triggers a retry (errors fed back to the LLM); ``obj`` may be mutated
# (e.g. clamp a value). Registered via register_validator.
Validator = Callable[[Dict[str, Any], Optional[Dict[str, Any]]],
                     Tuple[bool, list, Dict[str, Any]]]
VALIDATORS: Dict[str, Validator] = {}


def register_validator(name: str, fn: Validator) -> None:
    VALIDATORS[name] = fn


class SkillNotFound(KeyError):
    """Raised when run_skill is asked for a skill that has no file, no overlay,
    and no registered fallback — and the caller passed no fallback_system."""


def run_skill(
    name: str,
    user_content: str,
    settings: Optional[Settings] = None,
    *,
    fallback_system: Optional[str] = None,
    output: Optional[str] = None,
    max_rounds_override: Optional[int] = None,
    context_data: Optional[Dict[str, Any]] = None,
    llm: Optional[Any] = None,
) -> Any:
    """Run a skill by name. Returns:
      * for ``output: json`` — the parsed dict, or ``{"_error": ..., "_raw": ...}``
        if it never parsed (the caller decides how to handle a failed skill),
      * for ``output: text`` — the raw reply string.

    ``fallback_system``: if the skill name is unknown (no file + no registered
    fallback), synthesize a chat skill from this system prompt — so existing
    LLM-call methods can adopt run_skill without forcing a skill file to exist
    (the hardcoded SYSTEM constant is the safety net). Without a fallback the
    call raises ``SkillNotFound``.

    ``llm``: an existing ``LLMClient`` to use for the chat backend (skips
    constructing one). Pass the caller's ``self`` so tests that patch its
    ``chat`` method keep working, and so a caller-configured client (auth,
    base url, ...) is reused. When None, a fresh ``LLMClient(settings)`` is
    built (with the skill's model override applied, if any).
    """
    settings = settings or get_settings()
    skill = get_skill(name, settings)
    if skill is None:
        if fallback_system is None:
            raise SkillNotFound(name)
        skill = Skill(name=name, system=fallback_system, backend="chat",
                      output=(output or "json"), max_rounds=1)
    out = (output or skill.output or "json").lower()
    rounds = max_rounds_override or skill.max_rounds or 1
    if skill.backend == "codex":
        return _run_codex(skill, user_content, settings, out)
    return _run_chat(skill, user_content, settings, out, rounds, context_data, llm)


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------
def _run_chat(skill: Skill, user_content: str, settings: Settings,
              out: str, rounds: int,
              context_data: Optional[Dict[str, Any]],
              llm: Optional[Any] = None) -> Any:
    # lazy import — client.py imports this module (via the LLM-call methods),
    # so a module-level import here would cycle.
    from .client import LLMClient, _extract_json
    # use the caller's LLMClient when provided (so a patched `chat` is honored);
    # else build a fresh one, applying the skill's per-skill model override.
    if llm is None:
        s = replace(settings, llm_model=skill.model) if skill.model else settings
        llm = LLMClient(s)
    messages = [{"role": "system", "content": skill.system},
                {"role": "user", "content": user_content}]
    validator = VALIDATORS[skill.validator] if skill.validator else None
    last_raw = ""
    for attempt in range(rounds):
        try:
            raw = llm.chat(messages)
        except Exception as e:
            # chat-level failure (gateway down / network) — distinct from
            # bad-JSON so a caller can preserve its own error wording (the
            # existing seven_r_strategy says "MigraQ error: ..." here).
            return {"_error": f"MigraQ error: {e!r}", "_kind": "chat_error", "_raw": ""}
        last_raw = raw or ""
        if out == "text":
            return raw
        obj = _extract_json(raw)
        if obj is None:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             "Your previous output was not valid JSON. Output "
                             "ONLY the JSON object, no prose, no markdown fences."})
            continue
        if validator is not None:
            ok, errs, obj = validator(obj, context_data)
            if not ok and attempt < rounds - 1:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                                 "The validator rejected this:\n- "
                                 + "\n- ".join(errs)
                                 + "\n\nFix every error and re-emit ONLY the JSON object."})
                continue
        return obj
    return {"_error": "MigraQ output was not valid JSON", "_kind": "bad_json",
            "_raw": last_raw[:800]}


def _skill_resource_preamble(skill: Skill) -> str:
    """Preamble listing a directory-form skill's bundled files so the agent
    knows they're in its workspace and how to use them. References are read for
    context (Read / ``cat``); scripts run only in execute mode (the read-only
    plan sandbox blocks ``python`` execution)."""
    lines = ["--- SKILL RESOURCES ---",
             "This workspace bundles reference files and scripts for this skill:",
             "  references/<file>  — read for context (Read or `cat references/<file>`)",
             "  scripts/<file>     — python scripts; run in EXECUTE mode via "
             "`python scripts/<file>` (read-only in plan mode)",
             "Files:"]
    for f in skill.files:
        if f.kind == "reference":
            tag = "read"
        elif skill.mode == "execute":
            tag = "run"
        else:
            tag = "read-only (execute mode needed to run)"
        lines.append(f"  {f.path}  [{tag}]")
    lines.append("Ground your work in these files before deciding.")
    lines.append("--- END SKILL RESOURCES ---")
    return "\n".join(lines)


def _stage_skill_files(skill: Skill) -> Optional[str]:
    """Copy a directory-form skill's bundled files into a fresh temp workspace
    the agent runs in. Returns the workspace path (also passed via add_dirs) so
    the agent can Read references and run scripts. None if the skill has no
    files or staging failed (caller falls back to the plain system prompt)."""
    import shutil
    import tempfile
    from pathlib import Path
    if not skill.files or not skill.dir:
        return None
    tmp = tempfile.mkdtemp(prefix="skill_ws_")
    try:
        for f in skill.files:
            src = Path(skill.dir) / f.path
            dst = Path(tmp) / f.path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dst)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return tmp


def _run_codex(skill: Skill, user_content: str, settings: Settings,
               out: str) -> Any:
    # lazy import — agent/__init__ doesn't import client, but keep the import
    # lazy so the runner stays cheap to load.
    from ..agent import run_agent_sync
    # per-skill model override (codex backend uses codex_model)
    s = replace(settings, codex_model=skill.model) if skill.model else settings
    mode = "execute" if skill.mode == "execute" else "plan"
    # Round-1: stage the skill's bundled reference/script files into a temp
    # workspace the agent runs in, and prepend a manifest preamble so it knows
    # they're there. Falls back to the plain system prompt if staging fails.
    import shutil
    ws = _stage_skill_files(skill)
    context = skill.system
    cwd = None
    add_dirs = None
    if ws is not None:
        context = _skill_resource_preamble(skill) + "\n\n" + skill.system
        cwd = ws
        add_dirs = [ws]
    try:
        res = run_agent_sync(user_content, settings=s, mode=mode, context=context,
                             cwd=cwd, add_dirs=add_dirs)
    finally:
        if ws is not None:
            shutil.rmtree(ws, ignore_errors=True)
    if out == "text":
        return res.output
    from .client import _extract_json
    obj = _extract_json(res.output)
    if obj is None:
        return {"_error": f"codex skill returned no valid JSON (status={res.status})",
                "_raw": (res.output or res.error)[:800]}
    return obj


# ---------------------------------------------------------------------------
# round 3: try_skill — run a skill WITHOUT saving, for the Skills-page Try
# panel. If ``markdown`` is given it's parsed as a transient Skill so the
# operator can try an unsaved edit before committing. Returns a dict:
#   {ok, kind, output, error, resources_touched}
# kind ∈ {"json","text","codex","wired","not_found","bad_markdown","chat_error","bad_json"}.
# resources_touched = bundled files the codex agent read/ran (mined from tool
# events), so the UI can badge them "used in last run". Structured-input skills
# (seven_r/audit/assess/review) reject with kind="wired" — try those from the
# Inventory drawer's AI buttons where the input is a real host.
# ---------------------------------------------------------------------------
def try_skill(name: str, user_content: str,
              settings: Optional[Settings] = None,
              *, markdown: Optional[str] = None,
              llm: Optional[Any] = None) -> Dict[str, Any]:
    from .skills import _parse_skill_text, _STRUCTURED_INPUT
    settings = settings or get_settings()
    if markdown:
        skill = _parse_skill_text(markdown)
        if skill is None:
            return {"ok": False, "kind": "bad_markdown",
                    "error": "markdown did not parse (need --- frontmatter --- + body)"}
        skill.name = name  # the URL name wins over a mismatched name: in the body
    else:
        skill = get_skill(name, settings)
        if skill is None:
            return {"ok": False, "kind": "not_found", "error": "no such skill"}
    if name in _STRUCTURED_INPUT:
        return {"ok": False, "kind": "wired",
                "error": "This skill takes structured input (a host/app). Try it "
                         "from the Inventory drawer's AI buttons, not free text."}
    if skill.backend == "codex":
        return _try_codex(skill, user_content, settings)
    return _try_chat(skill, user_content, settings, llm)


def _try_chat(skill: Skill, user_content: str, settings: Settings,
              llm: Optional[Any]) -> Dict[str, Any]:
    from .client import LLMClient, _extract_json
    if llm is None:
        s = replace(settings, llm_model=skill.model) if skill.model else settings
        llm = LLMClient(s)
    messages = [{"role": "system", "content": skill.system},
                {"role": "user", "content": user_content}]
    try:
        raw = llm.chat(messages)
    except Exception as e:
        return {"ok": False, "kind": "chat_error",
                "error": f"chat error: {e!r}"}
    if skill.output == "text":
        return {"ok": True, "kind": "text", "output": raw, "resources_touched": []}
    obj = _extract_json(raw)
    if obj is None:
        return {"ok": False, "kind": "bad_json",
                "error": "no valid JSON in reply", "raw": (raw or "")[:800],
                "resources_touched": []}
    return {"ok": True, "kind": "json", "output": obj, "resources_touched": []}


def _try_codex(skill: Skill, user_content: str,
               settings: Settings) -> Dict[str, Any]:
    import shutil
    from ..agent import run_agent_sync
    s = replace(settings, codex_model=skill.model) if skill.model else settings
    mode = "execute" if skill.mode == "execute" else "plan"
    ws = _stage_skill_files(skill)
    context = skill.system
    cwd = None
    add_dirs = None
    if ws is not None:
        context = _skill_resource_preamble(skill) + "\n\n" + skill.system
        cwd = ws
        add_dirs = [ws]
    try:
        res = run_agent_sync(user_content, settings=s, mode=mode,
                             context=context, cwd=cwd, add_dirs=add_dirs)
    finally:
        if ws is not None:
            shutil.rmtree(ws, ignore_errors=True)
    # mine tool events for bundled files the agent read/ran (file path appears
    # in the command or file_change text) → "used in last run" badges
    touched = set()
    for ev in getattr(res, "events", []) or []:
        if ev.kind != "tool":
            continue
        txt = ev.text or ""
        for f in skill.files:
            if f.path in txt:
                touched.add(f.path)
    ok = res.status == "done" and not res.error
    return {"ok": ok, "kind": "codex", "output": res.output,
            "error": res.error, "resources_touched": sorted(touched)}


# ---------------------------------------------------------------------------
# round 4: run_skill_script — run a skill's bundled python script (the
# editor's CURRENT content, unsaved OK) in a sandbox and capture stdout/stderr.
# For the Skills-page Run button — lets an operator debug a script online
# before saving. Stages the skill's other bundled files alongside so the script
# can read references/ or import sibling scripts/. Sandboxed: tight timeout,
# minimal env (NO secrets inherited — the backend's IDC_DB_URL /
# ANTHROPIC_AUTH_TOKEN / web password never reach the child), capped output.
# Rejects chat-backend skills (they can't have scripts) + non-.py paths.
# Operator-only at the API layer (POST /api/skills/{name}/run-script, session).
# ---------------------------------------------------------------------------
_RUN_TIMEOUT_S = 10
_RUN_OUT_CAP = 65536


def run_skill_script(name: str, rel_path: str, content: str,
                     settings: Optional[Settings] = None) -> Dict[str, Any]:
    from .skills import get_skill, _validate_bundle_path, ensure_codex_skill
    import os, shutil, subprocess, sys, tempfile, time
    from pathlib import Path
    settings = settings or get_settings()
    skill = get_skill(name, settings)
    if skill is None:
        return {"ok": False, "error": "no such skill"}
    kind, _bn = _validate_bundle_path(rel_path)
    if kind != "script":
        return {"ok": False, "error": "path must be scripts/<name>.py"}
    # scripts only run for codex skills (chat has no tool use) — switch
    # chat→codex first (materialize/flip the overlay SKILL.md) instead of
    # rejecting, same as write_skill_file. If no overlay dir is configured
    # the skill stays chat and we can't run — surface a clear error.
    if skill.backend != "codex":
        skill = ensure_codex_skill(name, settings)
        if skill is None:
            return {"ok": False, "error": "no such skill"}
        if skill.backend != "codex":
            return {"ok": False, "error":
                    "editing disabled — set IDC_SKILLS_DIR to switch this skill "
                    "to codex before running its scripts"}
    # stage the skill's bundled files (so sibling reads/imports work), then
    # overwrite the target with the editor's current (possibly unsaved) content
    tmp = tempfile.mkdtemp(prefix="skill_run_")
    try:
        if skill.dir:
            for f in skill.files:
                src = Path(skill.dir) / f.path
                dst = Path(tmp) / f.path
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_file():
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass
        target = Path(tmp) / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # minimal env — do NOT inherit the backend's secrets into the child
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "LANG": "C.UTF-8", "HOME": str(tmp),
               "PYTHONUNBUFFERED": "1"}
        t0 = time.monotonic()
        try:
            proc = subprocess.run([sys.executable, rel_path], cwd=tmp,
                                  env=env, capture_output=True, text=True,
                                  timeout=_RUN_TIMEOUT_S)
            return {"ok": True, "exit_code": proc.returncode,
                    "stdout": (proc.stdout or "")[-_RUN_OUT_CAP:],
                    "stderr": (proc.stderr or "")[-_RUN_OUT_CAP:],
                    "duration_ms": int((time.monotonic() - t0) * 1000)}
        except subprocess.TimeoutExpired as e:
            def _tail(v):
                v = v or ""
                return v[-_RUN_OUT_CAP:] if isinstance(v, str) else ""
            return {"ok": False, "error": f"timeout (>{_RUN_TIMEOUT_S}s)",
                    "stdout": _tail(e.stdout), "stderr": _tail(e.stderr),
                    "duration_ms": _RUN_TIMEOUT_S * 1000}
        except Exception as e:
            return {"ok": False, "error": f"run failed: {e!r}",
                    "duration_ms": 0}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)