"""Pluggable skill loader for idc-migrate's LLM features.

A **skill** is a markdown file with YAML frontmatter + a system-prompt body:

  ---
  name: seven_r
  description: Assign a 7R migration strategy to one app
  backend: chat            # chat = LLMClient.chat (single-shot) | codex = the
                           # codex agent harness (read-only or workspace-write)
  model: glm-5.2:cloud      # optional per-skill model override
  mode: plan               # codex only: plan (read-only) | execute (workspace-write)
  output: json             # json | text
  schema: {...}            # optional, descriptive (documents the expected shape)
  validator: some_fn       # optional, name in the runner's VALIDATORS registry
  max_rounds: 1           # propose→validate→loop rounds (1 = no loop)
  ---
  <system prompt body — fed to the LLM/agent as the system message>

A skill lives as a **directory**:

  idc/skills/<name>/SKILL.md     # frontmatter + body (the body IS the system prompt)
                references/*.md   # bundled reference files (read by the agent)
                scripts/*.py      # bundled python scripts (run in execute mode)

The directory form is the ONLY on-disk form — no single-file ``<name>.md``
variant. The codex backend stages the bundled files into the agent workspace
and lists them in a preamble so the agent can Read them / run the scripts; the
chat backend has no tool use so it ignores bundled files. ``write_skill_file``
therefore SWITCHES a chat-backend skill to codex first (materializing/rewriting
the overlay ``SKILL.md`` with ``backend: codex``, body + other frontmatter
preserved) rather than rejecting — the operator asked to bundle a file, which
only the codex path uses, so the backend flips to match.

Skills load from two dirs (the overlay wins by name): the built-in
``idc/skills/<name>/SKILL.md`` + an operator overlay pointed at by
``IDC_SKILLS_DIR``. A skill absent from BOTH but registered in ``_FALLBACKS``
still resolves — synthesized from the registered system prompt. Only the
loop-driven skills (``lz_design`` / ``network_policy`` / ``waveplan``) still
register a ``_FALLBACK``: their prompts are Python constants used directly by
their (not-yet-skill-wired) methods, with no shipped file. The four wired
skills (``seven_r`` / ``audit_match`` / ``assess_wave`` / ``review_plan``) ship
``SKILL.md`` files as their single source of truth — no duplicate Python
constant, no drift guard.

This is the idc-migrate analogue of Claude Code's ``SKILL.md`` system: the
backend's LLM calls become skill invocations (see ``idc.llm.runner.run_skill``),
and a skill picks its backend (chat vs codex) — unifying the LLMClient and
codex agent paths under one abstraction.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from yaml import safe_load

from ..config import get_settings

# Built-in skills live next to the idc package (idc/skills/*.md). An operator
# overlay dir (IDC_SKILLS_DIR, empty = none) is loaded AFTER the built-ins and
# overrides by name — the edit surface for customizing behavior without code.
_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class SkillFile:
    """One bundled file inside a directory-form skill (round 1). ``path`` is
    relative to the skill dir (``references/foo.md`` / ``scripts/bar.py``);
    ``kind`` is ``reference`` (read for context) or ``script`` (run in execute
    mode). Content is read on demand via :func:`read_skill_file` — never held in
    the dataclass, so a large reference doesn't bloat the cached registry."""
    path: str
    kind: str        # "reference" | "script"
    size: int


@dataclass
class Skill:
    name: str
    system: str                     # the body = the system prompt
    description: str = ""
    backend: str = "chat"            # "chat" | "codex"
    model: str = ""                  # optional per-skill model override
    mode: str = "plan"               # codex only: "plan" (read-only) | "execute"
    output: str = "json"             # "json" | "text"
    schema: Dict[str, Any] = field(default_factory=dict)   # descriptive
    validator: str = ""              # name in runner.VALIDATORS, or ""
    max_rounds: int = 1
    # the skill's bundle dir + its bundled files. Empty / None for _FALLBACKS
    # skills (no dir). The codex backend stages these into the agent workspace;
    # the chat backend ignores them (and write_skill_file switches a chat skill
    # to codex before attaching, since only codex reads bundled files).
    files: List[SkillFile] = field(default_factory=list)
    dir: Optional[str] = None        # absolute path to the skill dir, or None


# Fallbacks for built-in skills that have no file on disk (the file overrides
# this when present). Populated via register_fallback so this module never
# imports the SYSTEM constants from client.py (would be a cycle: client.py
# imports the runner, which imports this). Each entry: name -> system prompt.
_FALLBACKS: Dict[str, str] = {}
# whether the on-disk registry has been loaded this process (reload on reset)
_registry: Optional[Dict[str, Skill]] = None

# Where each built-in skill takes effect, so the Skills UI can tell the operator
# what they're editing (an operator sees "seven_r" but doesn't know it's the 7R
# button in the host drawer). Human-readable; not parsed. A custom (overlay)
# skill the operator adds won't have an entry here — the UI shows "custom".
_USED_BY: Dict[str, str] = {
    "seven_r": "7R strategy — host drawer 'AI 7R' + CLI `idc 7r` + /api/7r-strategy",
    "audit_match": "Match audit — host drawer 'audit match' + CLI `idc audit` + /api/audit",
    "assess_wave": "Wave runbook — Waves page 'assess wave' + /api/assess-wave (go/no-go + pre-checks/cutover/rollback)",
    "review_plan": "Plan red-team — Waves page 'review plan' + /api/review-plan (semantic issues)",
    "lz_design": "Landing Zone design interview — Landing Zone page 'AI design' (account/VPC blueprint)",
    "network_policy": "Network designer policy — Landing Zone page 'AI subnet policy' (carving)",
    "waveplan": "Wave policy — Waves page 'plan with AI' (NL demand → stages/filters)",
}


# Skills whose input is structured (a host/app object built by an LLMClient
# method) rather than free text the operator types — the Skills-page Try panel
# rejects these and points to the Inventory drawer's AI buttons instead. The
# NL-demand skills (lz_design/network_policy/waveplan) take free text, so they
# ARE tryable; codex + custom skills are tryable too.
_STRUCTURED_INPUT = {"seven_r", "audit_match", "assess_wave", "review_plan"}


def register_fallback(name: str, system_prompt: str) -> None:
    """Register the built-in default system prompt for a skill with no file.

    Called by idc/llm/__init__.py for the existing SYSTEM constants, so the
    hardcoded prompts remain the out-of-the-box behavior and a skill FILE
    (built-in or overlay) overrides them. Keeps seven_r / waveplan / ... working
    unchanged while making them skill-editable."""
    _FALLBACKS[name] = system_prompt


def _parse_skill(path: Path) -> Optional[Skill]:
    """Parse one ``.md`` skill file (frontmatter + body). None on malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    # split frontmatter (between the first two --- lines) from the body
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = safe_load(parts[1]) or {}
    except Exception:
        return None
    body = parts[2].strip()
    if not isinstance(meta, dict) or not body:
        return None
    name = str(meta.get("name") or path.stem).strip()
    return Skill(
        name=name,
        system=body,
        description=str(meta.get("description") or "").strip(),
        backend=str(meta.get("backend") or "chat").strip().lower(),
        model=str(meta.get("model") or "").strip(),
        mode=str(meta.get("mode") or "plan").strip().lower(),
        output=str(meta.get("output") or "json").strip().lower(),
        schema=meta.get("schema") if isinstance(meta.get("schema"), dict) else {},
        validator=str(meta.get("validator") or "").strip(),
        max_rounds=int(meta.get("max_rounds") or 1),
    )


# subdirs of a skill dir that hold bundled files (round 1). The subdir a file
# lives in determines its ``kind``: references/ → "reference", scripts/ →
# "script". Flat only (no nested subdirs) for round 1; round 2 (UI management)
# can extend to recursive if needed.
_BUNDLE_SUBDIRS = (("references", "reference"), ("scripts", "script"))


def _scan_skill_files(skill_dir: Path) -> List[SkillFile]:
    """List the bundled reference/script files in a skill dir (flat, sorted)."""
    out: List[SkillFile] = []
    for sub, kind in _BUNDLE_SUBDIRS:
        d = skill_dir / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append(SkillFile(path=f"{sub}/{p.name}", kind=kind, size=size))
    return out


def _parse_skill_dir(skill_dir: Path) -> Optional[Skill]:
    """Parse a directory-form skill (``<dir>/SKILL.md`` + bundled files). None
    if no SKILL.md or it's malformed. The directory form overrides a same-named
    single-file skill in the same source dir (more specific)."""
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    s = _parse_skill(md)
    if s is None:
        return None
    s.dir = str(skill_dir)
    s.files = _scan_skill_files(skill_dir)
    return s


def _load_registry(settings=None) -> Dict[str, Skill]:
    """Load built-in skills + overlay, then fill missing built-ins from
    _FALLBACKS. The ONLY on-disk form is a directory ``<name>/SKILL.md``;
    the overlay overrides the built-in by name; _FALLBACKS only fills gaps
    (a file always beats the fallback)."""
    out: Dict[str, Skill] = {}
    overlay = (settings or get_settings()).skills_dir
    source_dirs: List[Path] = []
    if _BUILTIN_DIR.is_dir():
        source_dirs.append(_BUILTIN_DIR)
    if overlay:
        od = Path(overlay)
        if od.is_dir():
            source_dirs.append(od)   # loaded after builtin → wins by name
    for source_dir in source_dirs:
        for p in sorted(source_dir.glob("*/SKILL.md")):
            s = _parse_skill_dir(p.parent)
            if s:
                out[s.name] = s
    # fill gaps from _FALLBACKS so known skills always resolve even without a file
    for name, sys_prompt in _FALLBACKS.items():
        if name not in out:
            out[name] = Skill(name=name, system=sys_prompt, backend="chat",
                              output="json", max_rounds=1)
    return out


def _registry_cached(settings=None) -> Dict[str, Skill]:
    global _registry
    if _registry is None:
        _registry = _load_registry(settings)
    return _registry


def get_skill(name: str, settings=None) -> Optional[Skill]:
    """Resolve a skill by name (built-in file → overlay → _FALLBACKS).
    None if no file AND no registered fallback — the caller decides whether
    that's an error or a chance to pass its own fallback_system."""
    return _registry_cached(settings).get(name)


def list_skills(settings=None) -> Dict[str, Skill]:
    """All resolved skills (built-in + overlay + fallbacks), by name."""
    return dict(_registry_cached(settings))


def reset_registry() -> None:
    """Force a re-scan of the skill dirs on next get_skill (used by tests)."""
    global _registry
    _registry = None


# ---------------------------------------------------------------------------
# higher-level views + mutations for the /api/skills UI (list / read / edit /
# reset). Editing writes ONLY to the overlay dir (IDC_SKILLS_DIR) — never to the
# repo's idc/skills/ (that's release content). reset_registry() is called after
# every write/delete so the next run_skill picks up the change with no restart.
# ---------------------------------------------------------------------------
def _overlay_dir(settings=None) -> Optional[Path]:
    od = (settings or get_settings()).skills_dir
    return Path(od) if od else None


def _source_of(name: str, settings=None) -> Tuple[str, Optional[str]]:
    """('overlay'|'builtin'|'fallback', file_path). overlay beats builtin; the
    only on-disk form is ``<name>/SKILL.md``; fallback when no file at all
    (synthesized from _FALLBACKS). ``file_path`` points at the SKILL.md."""
    od = _overlay_dir(settings)
    if od is not None:
        if (od / name / "SKILL.md").is_file():
            return "overlay", str(od / name / "SKILL.md")
    if (_BUILTIN_DIR / name / "SKILL.md").is_file():
        return "builtin", str(_BUILTIN_DIR / name / "SKILL.md")
    return "fallback", None


def _default_body(name: str, settings=None) -> str:
    """The built-in body an operator's overlay is diffed against / reset to:
    the ``_FALLBACK`` constant when one is registered (the loop-driven skills),
    else the shipped ``idc/skills/<name>/SKILL.md`` body, else '' (a custom
    skill with no built-in default)."""
    if name in _FALLBACKS:
        return _FALLBACKS[name]
    p = _BUILTIN_DIR / name / "SKILL.md"
    if p.is_file():
        s = _parse_skill(p)
        if s is not None:
            return s.system
    return ""


def list_skill_infos(settings=None) -> list:
    """One compact dict per resolved skill, for the UI list."""
    out = []
    for name, s in sorted(_registry_cached(settings).items()):
        source, file_path = _source_of(name, settings)
        out.append({
            "name": s.name, "description": s.description, "backend": s.backend,
            "model": s.model, "mode": s.mode, "output": s.output,
            "max_rounds": s.max_rounds, "validator": s.validator,
            "source": source, "file_path": file_path,
            "editable": _overlay_dir(settings) is not None,
            # a built-in ships a file or registers a _FALLBACK — i.e. it exists
            # independent of the operator's overlay. The UI groups these as
            # "built-in" vs operator-added "custom".
            "overriding_default": source in ("builtin", "fallback"),
            "used_by": _USED_BY.get(name, ""),
            "files": [{"path": f.path, "kind": f.kind, "size": f.size}
                      for f in s.files],
        })
    return out


def skill_view(name: str, settings=None) -> Optional[dict]:
    """Full info for one skill: the effective body (what's currently used),
    parsed frontmatter, source, file_path, and the default_body (the built-in
    body the overlay is diffed against / reset to — _FALLBACK constant or
    shipped SKILL.md body) so the UI can show a diff + offer 'reset to default'.
    None if the skill name doesn't resolve (no file + no registered fallback)."""
    s = get_skill(name, settings)
    if s is None:
        return None
    source, file_path = _source_of(name, settings)
    # the editable markdown: the actual file content when one exists, else a
    # canonical reconstruction from the parsed fields + body (so the operator
    # can edit a fallback skill too — saving creates the overlay file).
    if file_path:
        try:
            markdown = Path(file_path).read_text(encoding="utf-8")
        except OSError:
            markdown = _reconstruct_markdown(s)
    else:
        markdown = _reconstruct_markdown(s)
    return {
        "name": s.name, "description": s.description, "backend": s.backend,
        "model": s.model, "mode": s.mode, "output": s.output,
        "max_rounds": s.max_rounds, "validator": s.validator, "schema": s.schema,
        "system": s.system,            # the effective system prompt body
        "markdown": markdown,          # what the editor loads (file content or reconstructed)
        "source": source, "file_path": file_path,
        "editable": _overlay_dir(settings) is not None,
        "overriding_default": source in ("builtin", "fallback"),
        # the built-in body the overlay is diffed against / reset to: the
        # _FALLBACK constant (loop-driven skills) or the shipped SKILL.md body.
        "default_body": _default_body(name, settings),
        "used_by": _USED_BY.get(name, ""),
        "files": [{"path": f.path, "kind": f.kind, "size": f.size}
                  for f in s.files],
        "dir": s.dir,
    }


def _reconstruct_markdown(s: Skill) -> str:
    """Build a canonical markdown file from a parsed Skill (for editing a
    fallback skill that has no file). Only emits non-default optional fields."""
    lines = ["---",
             f"name: {s.name}",
             f'description: "{s.description}"' if s.description else "description: ''",
             f"backend: {s.backend}"]
    if s.model:
        lines.append(f"model: {s.model}")
    if s.backend == "codex":
        lines.append(f"mode: {s.mode}")
    lines.append(f"output: {s.output}")
    if s.max_rounds != 1:
        lines.append(f"max_rounds: {s.max_rounds}")
    if s.validator:
        lines.append(f"validator: {s.validator}")
    lines.append("---")
    lines.append("")
    lines.append(s.system)
    return "\n".join(lines)


def write_skill(name: str, markdown: str, settings=None) -> str:
    """Write (create or overwrite) a skill's SKILL.md in the overlay dir
    (``<name>/SKILL.md`` — the only on-disk form). Validates it parses
    (frontmatter + non-empty body + the name matches). Raises ValueError if
    no overlay dir is configured or the markdown is malformed. reset_registry()
    so the next run_skill picks it up immediately."""
    od = _overlay_dir(settings)
    if od is None:
        raise ValueError("editing disabled — set IDC_SKILLS_DIR to a writable "
                         "dir and restart the backend")
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid skill name")
    parsed = _parse_skill_text(markdown, expected_name=name)
    if parsed is None:
        raise ValueError("markdown must be ---\\n<yaml frontmatter>\\n---\\n"
                         "<non-empty system prompt body>, with name: " + name)
    skill_dir = od / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    # one-step undo: snapshot the previous overlay SKILL.md next to the live
    # file before overwriting (round 3). undo_skill restores it. Only snapshots
    # when an overlay SKILL.md already exists (not on the first save).
    if path.is_file():
        try:
            shutil.copy2(str(path), str(path.with_name(path.name + ".prev")))
        except OSError:
            pass
    path.write_text(markdown, encoding="utf-8")
    reset_registry()
    return str(path)


def delete_skill(name: str, settings=None) -> bool:
    """Remove the overlay skill dir (reverts to the builtin file or the
    _FALLBACK constant). The whole ``<name>/`` dir is the overlay artifact
    (SKILL.md + any bundled references/scripts). Returns whether anything was
    removed. reset_registry."""
    od = _overlay_dir(settings)
    if od is None:
        return False
    d = od / name
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    reset_registry()
    return True


def read_skill_file(name: str, rel_path: str,
                    settings=None) -> Optional[Tuple[str, str, int]]:
    """Return ``(content, kind, size)`` for one bundled file of a directory-form
    skill, or None if the skill has no directory form / the path isn't one of
    its bundled reference or script files. ``rel_path`` must be a path listed in
    the skill's ``files`` (e.g. ``references/foo.md``) — the authoritative file
    list is the allowlist, so path traversal (``../``, absolute paths) can't
    reach anything the operator didn't bundle. Round-1 read-only surface for
    the UI; round 2 adds write/delete."""
    s = get_skill(name, settings)
    if s is None or not s.dir:
        return None
    match = next((f for f in s.files if f.path == rel_path), None)
    if match is None:
        return None
    try:
        content = (Path(s.dir) / rel_path).read_text(encoding="utf-8")
    except OSError:
        return None
    return content, match.kind, match.size


# ---------------------------------------------------------------------------
# round 2: write/delete a skill's bundled reference/script files (UI editor).
# A bundled-file path must be ``references/<basename>`` or ``scripts/<basename>``
# (flat, no nested dirs, no hidden files); scripts must be ``.py``. The regex is
# the allowlist — a path that doesn't match can't be written, so ``../`` and
# absolute paths can't escape the skill dir.
# ---------------------------------------------------------------------------
_BUNDLE_RE = re.compile(r"^(references|scripts)/([A-Za-z0-9._-]+)$")
_SCRIPT_EXT = ".py"


def _validate_bundle_path(rel_path: str) -> Tuple[Optional[str], str]:
    """Return ``(kind, basename)`` for a valid bundle path, else ``(None, "")``.
    ``references/<bn>`` → ``("reference", bn)``; ``scripts/<bn>`` →
    ``("script", bn)`` only when ``bn`` ends in ``.py``. Rejects traversal,
    nested dirs, and hidden files."""
    m = _BUNDLE_RE.match(rel_path or "")
    if not m:
        return None, ""
    sub, bn = m.group(1), m.group(2)
    if bn.startswith("."):
        return None, ""
    if sub == "scripts" and not bn.endswith(_SCRIPT_EXT):
        return None, ""
    return ("reference" if sub == "references" else "script"), bn


def _force_codex(skill: Skill) -> Skill:
    """Return a copy of ``skill`` with ``backend`` forced to ``codex`` (and a
    default ``mode: plan`` when unset), so a chat builtin/fallback can be
    materialized as a codex overlay when the operator attaches a bundled file.
    Bundled files are codex-only, so the chat→codex flip is the whole point."""
    return replace(skill, backend="codex", mode=skill.mode or "plan")


def _flip_overlay_backend_to_codex(sk_md: Path) -> None:
    """Rewrite an existing overlay ``SKILL.md`` in place, flipping ``backend:``
    to ``codex`` and inserting ``mode: plan`` when missing. Preserves the body
    and every other frontmatter field verbatim (the operator's prompt + schema
    + validator + max_rounds + ... are untouched). No-op if the file is already
    codex or unparseable — the bundled file still writes; a chat skill left
    here just ignores it, which the operator can fix by editing the SKILL.md."""
    try:
        text = sk_md.read_text(encoding="utf-8")
    except OSError:
        return
    if not text.startswith("---"):
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        return
    fm = parts[1]
    if re.search(r"(?m)^backend:\s*codex\s*$", fm):
        return  # already codex
    fm = re.sub(r"(?m)^backend:\s*\S+\s*$", "backend: codex", fm, count=1)
    if not re.search(r"(?m)^mode:", fm):
        fm = fm.rstrip("\n") + "\nmode: plan\n"
    sk_md.write_text("---" + fm + "---" + parts[2], encoding="utf-8")


def _materialize_codex_overlay(name: str, cur: Optional[Skill],
                               sk_md: Path) -> None:
    """Write a fresh overlay ``SKILL.md`` forced to ``backend: codex``. ``cur``
    is the currently-resolved skill (a chat builtin/fallback/overlay). Copy its
    source file verbatim then flip (preserves schema + comments), or reconstruct
    from a ``_FALLBACK`` (no dir), or emit a codex stub when ``cur`` is None (a
    brand-new skill). Caller has already mkdir'd the skill dir."""
    src_md = Path(cur.dir, "SKILL.md") if cur and cur.dir else None
    if src_md and src_md.is_file():
        sk_md.write_text(src_md.read_text(encoding="utf-8"), encoding="utf-8")
        _flip_overlay_backend_to_codex(sk_md)
    elif cur is not None:
        sk_md.write_text(_reconstruct_markdown(_force_codex(cur)),
                         encoding="utf-8")
    else:
        sk_md.write_text(f"---\nname: {name}\nbackend: codex\nmode: plan\n"
                         f"output: json\n---\n(skill {name})\n",
                         encoding="utf-8")


def ensure_codex_skill(name: str, settings=None) -> Optional[Skill]:
    """Resolve skill ``name`` and SWITCH it to codex if it's chat-backend:
    materialize the overlay ``SKILL.md`` (from a builtin file / _FALLBACK) or
    flip the existing overlay in place to ``backend: codex`` (body + other
    frontmatter preserved), ``reset_registry``, and return the re-resolved
    codex Skill. Idempotent: an already-codex skill is returned unchanged.

    The shared chat→codex switch used by every codex-only feature so the
    operator never hits a "chat skills can't ..." reject — attaching a bundled
    file (``write_skill_file``) AND running a bundled script
    (``runner.run_skill_script``) both flip via this. Returns None if no such
    skill; returns the chat skill UNCHANGED (no flip) when no overlay dir is
    configured — the caller decides whether to error (``write_skill_file``
    raises earlier; ``run_skill_script`` returns an error)."""
    cur = get_skill(name, settings)
    if cur is None:
        return None
    if cur.backend == "codex":
        return cur
    od = _overlay_dir(settings)
    if od is None:
        return cur  # caller handles (raise / error)
    skill_dir = od / name
    sk_md = skill_dir / "SKILL.md"
    if not sk_md.is_file():
        skill_dir.mkdir(parents=True, exist_ok=True)
        _materialize_codex_overlay(name, cur, sk_md)
    else:
        _flip_overlay_backend_to_codex(sk_md)
    reset_registry()
    return get_skill(name, settings)


def write_skill_file(name: str, rel_path: str, content: str,
                     settings=None) -> str:
    """Write (create or overwrite) one bundled reference/script file for a skill
    in the overlay dir. Bundled files only apply to codex skills (the chat path
    has no tool use), so if the resolved skill is chat-backend it is SWITCHED to
    codex first via ``ensure_codex_skill`` (materialize/flip the overlay
    ``SKILL.md`` with ``backend: codex``, body + other frontmatter preserved).
    Returns the file path. Raises ValueError if no overlay dir, the skill name
    is invalid, ``rel_path`` isn't a valid bundle path, or a script file doesn't
    compile. reset_registry so the next run_skill picks up the file (and the
    flip)."""
    od = _overlay_dir(settings)
    if od is None:
        raise ValueError("editing disabled — set IDC_SKILLS_DIR to a writable "
                         "dir and restart the backend")
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid skill name")
    kind, _bn = _validate_bundle_path(rel_path)
    if kind is None:
        raise ValueError("file path must be references/<name> or scripts/<name.py>")
    cur = get_skill(name, settings)
    # scripts must be syntactically valid python BEFORE we touch anything on
    # disk — a broken script only blows up when the codex agent runs it.
    if kind == "script":
        try:
            compile(content, rel_path, "exec")
        except SyntaxError as e:
            raise ValueError(f"script {rel_path} does not compile: {e}")
    skill_dir = od / name
    sk_md = skill_dir / "SKILL.md"
    if not sk_md.is_file():
        # no overlay yet — materialize one forced to codex (brand-new skill
        # gets a codex stub; existing chat skill copies its source then flips).
        skill_dir.mkdir(parents=True, exist_ok=True)
        _materialize_codex_overlay(name, cur, sk_md)
    elif cur is not None and cur.backend != "codex":
        # overlay SKILL.md exists but is chat-backend — flip it to codex in
        # place so the bundled file we're about to write actually takes effect.
        _flip_overlay_backend_to_codex(sk_md)
    sub = "references" if kind == "reference" else "scripts"
    (skill_dir / sub).mkdir(parents=True, exist_ok=True)
    fp = skill_dir / rel_path
    fp.write_text(content, encoding="utf-8")
    reset_registry()
    return str(fp)


def delete_skill_file(name: str, rel_path: str, settings=None) -> bool:
    """Remove one bundled file from a directory-form overlay skill. Returns
    whether a file was removed. No-op (False) if no overlay skill dir, the path
    isn't a currently-bundled file (allowlist), or the file is already gone.
    reset_registry."""
    od = _overlay_dir(settings)
    if od is None:
        return False
    skill_dir = od / name
    if not skill_dir.is_dir():
        return False
    # allowlist: only delete paths the loader currently lists as bundled
    s = get_skill(name, settings)
    if s is None or not any(f.path == rel_path for f in s.files):
        return False
    fp = skill_dir / rel_path
    if not fp.is_file():
        return False
    fp.unlink()
    reset_registry()
    return True


# ---------------------------------------------------------------------------
# round 3: one-step undo (the .prev snapshot written by write_skill). Lets an
# operator revert a bad prompt edit without remembering what changed.
# ---------------------------------------------------------------------------
def _overlay_live_path(name: str, settings=None) -> Optional[Path]:
    """The overlay's live ``<name>/SKILL.md`` for a skill, or None if no overlay
    file exists (builtin/fallback skills have no undo — there's no prior edit)."""
    od = _overlay_dir(settings)
    if od is None:
        return None
    if (od / name / "SKILL.md").is_file():
        return od / name / "SKILL.md"
    return None


def get_skill_prev(name: str, settings=None) -> Optional[str]:
    """The previous-saved markdown snapshot for a skill (the .prev file next to
    the live overlay), or None if no snapshot exists (skill never saved from an
    earlier overlay, or already undone). Round-3 read-only surface for the UI."""
    live = _overlay_live_path(name, settings)
    if live is None:
        return None
    prev = live.with_name(live.name + ".prev")
    if not prev.is_file():
        return None
    try:
        return prev.read_text(encoding="utf-8")
    except OSError:
        return None


def undo_skill(name: str, settings=None) -> Optional[str]:
    """Restore the .prev snapshot as the current overlay (one-step undo).
    Returns the restored markdown, or None if no snapshot exists. The snapshot
    is consumed (.prev removed) so undo is one-shot — save again to make a new
    snapshot. reset_registry so the restored body takes effect immediately."""
    live = _overlay_live_path(name, settings)
    if live is None:
        return None
    prev = live.with_name(live.name + ".prev")
    if not prev.is_file():
        return None
    try:
        md = prev.read_text(encoding="utf-8")
    except OSError:
        return None
    live.write_text(md, encoding="utf-8")
    try:
        prev.unlink()
    except OSError:
        pass
    reset_registry()
    return md


def _parse_skill_text(text: str, expected_name: str = "") -> Optional[Skill]:
    """Parse a markdown string into a Skill (used by write_skill to validate
    before writing). Same rules as _parse_skill but from text, not a path."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = safe_load(parts[1]) or {}
    except Exception:
        return None
    body = parts[2].strip()
    if not isinstance(meta, dict) or not body:
        return None
    name = str(meta.get("name") or expected_name).strip()
    if expected_name and name != expected_name:
        return None
    return Skill(
        name=name, system=body,
        description=str(meta.get("description") or "").strip(),
        backend=str(meta.get("backend") or "chat").strip().lower(),
        model=str(meta.get("model") or "").strip(),
        mode=str(meta.get("mode") or "plan").strip().lower(),
        output=str(meta.get("output") or "json").strip().lower(),
        schema=meta.get("schema") if isinstance(meta.get("schema"), dict) else {},
        validator=str(meta.get("validator") or "").strip(),
        max_rounds=int(meta.get("max_rounds") or 1),
    )