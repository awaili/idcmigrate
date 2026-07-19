"""Tests for the skill mechanism (idc/llm/skills.py loader + runner.py runner).

Covers: frontmatter parse (directory form is the ONLY on-disk form),
overlay-over-built-in precedence, _FALLBACKS gap-fill (the loop-driven skills),
run_skill chat backend (json parse + propose→validate→loop on bad-then-good
output + chat-exception wording), run_skill codex backend (mocked
run_agent_sync), fallback_system + SkillNotFound, the bundled-file write
behavior (attaching a file to a chat skill switches it to codex first;
scripts must compile), and that the four wired skills are driven by their
shipped SKILL.md (no Python constant).
"""
import json
import re
from pathlib import Path

import pytest

from idc.config import Settings
from idc.llm import skills as sk_mod
from idc.llm.runner import run_skill, SkillNotFound, register_validator
from idc.llm.skills import Skill, get_skill, list_skills, reset_registry, _BUILTIN_DIR


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test starts with a fresh registry scan so skills_dir changes +
    overlay writes take effect (the loader caches once per process)."""
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------
def test_seven_r_skill_file_parses():
    s = get_skill("seven_r")
    assert s is not None
    assert s.name == "seven_r"
    assert s.backend == "chat"
    assert s.output == "json"
    assert s.max_rounds == 1
    assert "cloud-migration strategist" in s.system


def test_four_wired_skills_ship_skillmd_no_constant():
    """The four wired skills resolve from their shipped <name>/SKILL.md, NOT
    from a _FALLBACK constant — the file is the single source of truth (no
    duplicate Python constant to drift against)."""
    from idc.llm.skills import _FALLBACKS
    for name in ("seven_r", "audit_match", "assess_wave", "review_plan"):
        s = get_skill(name)
        assert s is not None, f"{name} didn't resolve"
        assert (s.dir or "").endswith(name), f"{name} has no dir (not a shipped file)"
        assert name not in _FALLBACKS, f"{name} still registers a _FALLBACK constant"


def test_fallbacks_fill_gaps_for_loop_driven_skills():
    # lz_design has NO shipped file — it's registered as a _FALLBACK (its prompt
    # is still a Python constant used directly by design_lz; not yet skill-wired)
    s = get_skill("lz_design")
    assert s is not None and s.backend == "chat"
    assert "landing-zone architect" in s.system  # the LZ_DESIGN_SYSTEM body


def test_overlay_overrides_builtin(tmp_path, monkeypatch):
    # an overlay seven_r/SKILL.md with a custom body wins over the built-in
    d = tmp_path / "seven_r"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: seven_r\ndescription: custom\nbackend: chat\noutput: json\n"
        "max_rounds: 2\n---\nCUSTOM SEVEN R BODY\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    s = get_skill("seven_r", Settings(skills_dir=str(tmp_path)))
    assert s.system == "CUSTOM SEVEN R BODY"
    assert s.max_rounds == 2  # overlay's frontmatter wins too


def test_overlay_can_add_new_skill(tmp_path, monkeypatch):
    d = tmp_path / "my_audit"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: my_audit\ndescription: mine\nbackend: codex\nmode: plan\n"
        "output: json\n---\nDO THE THING\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    s = get_skill("my_audit", Settings(skills_dir=str(tmp_path)))
    assert s is not None and s.backend == "codex" and s.system == "DO THE THING"


# ---------------------------------------------------------------------------
# runner — chat backend
# ---------------------------------------------------------------------------
class _FakeLLM:
    """Stand-in for LLMClient: a callable .chat that returns canned replies
    (or raises). run_skill accepts `llm=` to use this instead of building one."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def chat(self, messages, stream=False, timeout=None):
        self.calls += 1
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_run_skill_chat_parses_json():
    llm = _FakeLLM(['{"strategy":"rehost","confidence":0.8}'])
    obj = run_skill("seven_r", "ctx", output="json", llm=llm)
    assert obj["strategy"] == "rehost" and obj["confidence"] == 0.8
    assert llm.calls == 1


def test_run_skill_chat_loops_on_bad_then_good_json():
    # max_rounds=3: first reply is garbage, second is valid → loop recovers
    llm = _FakeLLM(["not json at all",
                    '{"strategy":"replatform","confidence":0.6}'])
    obj = run_skill("seven_r", "ctx", max_rounds_override=3, output="json", llm=llm)
    assert obj["strategy"] == "replatform"
    assert llm.calls == 2  # one bad + one good


def test_run_skill_chat_exhausted_returns_error():
    llm = _FakeLLM(["nope", "still nope", "nope three"])
    obj = run_skill("seven_r", "ctx", max_rounds_override=3, output="json", llm=llm)
    assert "_error" in obj and obj["_kind"] == "bad_json"
    assert "not valid JSON" in obj["_error"]
    assert llm.calls == 3


def test_run_skill_chat_exception_returns_migraq_error():
    llm = _FakeLLM([RuntimeError("gateway down")])
    obj = run_skill("seven_r", "ctx", output="json", llm=llm)
    assert obj["_kind"] == "chat_error"
    assert "MigraQ error" in obj["_error"] and "gateway down" in obj["_error"]


def test_run_skill_text_output_returns_raw():
    llm = _FakeLLM(["plain text reply"])
    out = run_skill("seven_r", "ctx", output="text", llm=llm)
    assert out == "plain text reply"


def test_run_skill_fallback_system_when_skill_missing():
    # no skill file + no registered fallback → fallback_system synthesizes one
    llm = _FakeLLM(['{"x":1}'])
    obj = run_skill("nonexistent_skill", "ctx", fallback_system="SYS PROMPT",
                    output="json", llm=llm)
    assert obj == {"x": 1}


def test_run_skill_raises_when_missing_and_no_fallback():
    with pytest.raises(SkillNotFound):
        run_skill("totally_unknown", "ctx", output="json", llm=_FakeLLM([]))


# ---------------------------------------------------------------------------
# runner — codex backend
# ---------------------------------------------------------------------------
class _FakeAgentResult:
    def __init__(self, output, status="done"):
        self.output = output
        self.status = status
        self.error = ""


def test_run_skill_codex_backend_parses_json(tmp_path, monkeypatch):
    # a codex-backend skill via overlay
    d = tmp_path / "code_audit"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: code_audit\ndescription: x\nbackend: codex\nmode: plan\n"
        "output: json\n---\nAUDIT THE CODE\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc import agent
    monkeypatch.setattr(agent, "run_agent_sync",
                        lambda prompt, settings=None, **kw:
                        _FakeAgentResult('{"verdict":"change","confidence":0.9}'))
    obj = run_skill("code_audit", "look at the repo",
                    settings=Settings(skills_dir=str(tmp_path)))
    assert obj["verdict"] == "change" and obj["confidence"] == 0.9


def test_run_skill_codex_backend_text_output(tmp_path, monkeypatch):
    d = tmp_path / "code_text"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: code_text\ndescription: x\nbackend: codex\nmode: plan\n"
        "output: text\n---\nJUST TALK\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc import agent
    monkeypatch.setattr(agent, "run_agent_sync",
                        lambda prompt, settings=None, **kw:
                        _FakeAgentResult("free-form answer"))
    out = run_skill("code_text", "hi", settings=Settings(skills_dir=str(tmp_path)))
    assert out == "free-form answer"


# ---------------------------------------------------------------------------
# validator registry (propose→validate→loop)
# ---------------------------------------------------------------------------
def test_validator_rejects_then_accepts():
    # a validator that rejects the first obj (missing field) then accepts once
    # the LLM re-emits with the field. Demonstrates the loop wires to validators.
    calls = {"n": 0}

    def v(obj, ctx):
        calls["n"] += 1
        if "strategy" not in obj:
            return False, ["missing strategy"], obj
        return True, [], obj

    register_validator("test_v", v)
    # craft a temp skill with the validator + 2 rounds
    import tempfile, os
    d = tempfile.mkdtemp()
    try:
        os.makedirs(Path(d, "vskill"))
        Path(d, "vskill", "SKILL.md").write_text(
            "---\nname: vskill\ndescription: x\nbackend: chat\noutput: json\n"
            "validator: test_v\nmax_rounds: 2\n---\nSYS\n", encoding="utf-8")
        os.environ["IDC_SKILLS_DIR"] = d
        reset_registry()
        llm = _FakeLLM(['{"x":1}', '{"strategy":"rehost","confidence":0.7}'])
        obj = run_skill("vskill", "ctx", settings=Settings(skills_dir=d), llm=llm)
        assert obj["strategy"] == "rehost"
        assert calls["n"] == 2  # rejected once, accepted once
    finally:
        os.environ.pop("IDC_SKILLS_DIR", None)
        reset_registry()


# ---------------------------------------------------------------------------
# seven_r_strategy end-to-end through the skill path (regression: the
# existing tests in test_seven_r.py already cover behavior; this asserts the
# wiring goes via run_skill + the skill file, not the hardcoded constant)
# ---------------------------------------------------------------------------
def test_seven_r_strategy_uses_skill_file(tmp_path, monkeypatch):
    """An overlay that changes the 7R system prompt changes what's sent to
    chat — proving seven_r_strategy is skill-driven, not constant-driven."""
    from idc.llm.client import LLMClient
    from idc.core.models import Server, Utilization, Workload, CodeProfile
    d = tmp_path / "seven_r"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: seven_r\ndescription: x\nbackend: chat\noutput: json\n"
        "max_rounds: 1\n---\nCUSTOM MARKER PROMPT\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    sent = {}
    c = LLMClient(Settings(llm_enabled=True, skills_dir=str(tmp_path)))

    def fake_chat(messages, stream=False, timeout=None):
        sent["system"] = messages[0]["content"]
        return '{"strategy":"rehost","confidence":0.8,"rationale":"r","target":"CVM","effort":"low","key_changes":[]}'
    monkeypatch.setattr(c, "chat", fake_chat)
    s = Server(hostname="h", role="app", os="centos", app_ids=["a"],
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    r = c.seven_r_strategy("a", [s], [Workload(app_id="a", name="n", server_ids=[s.id])],
                           [], [CodeProfile(app_id="a")])
    assert r["ok"] is True and r["strategy"] == "rehost"
    assert sent["system"] == "CUSTOM MARKER PROMPT"  # overlay body was used


# ---------------------------------------------------------------------------
# audit_match / assess_wave / review_plan are skill-driven (overlay changes the
# system prompt sent to chat — proving they go via run_skill, not the constant)
# ---------------------------------------------------------------------------
def _overlay_skill(tmp_path, monkeypatch, name, body):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\nbackend: chat\noutput: json\n"
        f"max_rounds: 1\n---\n{body}\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()


def test_audit_match_uses_skill_file(tmp_path, monkeypatch):
    from idc.llm.client import LLMClient
    from idc.core.models import Server, Utilization
    from idc.core.match import match_server
    _overlay_skill(tmp_path, monkeypatch, "audit_match", "AUDIT MARKER")
    sent = {}
    c = LLMClient(Settings(llm_enabled=True, skills_dir=str(tmp_path)))

    def fake_chat(messages, stream=False, timeout=None):
        sent["system"] = messages[0]["content"]
        return '{"verdict":"keep","confidence":0.9,"critique":"c","risks":[]}'
    monkeypatch.setattr(c, "chat", fake_chat)
    s = Server(hostname="h", role="db", os="centos",
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    r = c.audit_match(s, match_server(s))
    assert r["ok"] and r["verdict"] == "keep"
    assert sent["system"] == "AUDIT MARKER"


def test_assess_wave_uses_skill_file(tmp_path, monkeypatch):
    from idc.llm.client import LLMClient
    from idc.core.models import Server, Utilization, Wave
    _overlay_skill(tmp_path, monkeypatch, "assess_wave", "ASSESS MARKER")
    sent = {}
    c = LLMClient(Settings(llm_enabled=True, skills_dir=str(tmp_path)))

    def fake_chat(messages, stream=False, timeout=None):
        sent["system"] = messages[0]["content"]
        return '{"go_no_go":"go","pre_checks":[],"cutover":["flip"],"rollback":["repoint"],"summary":"s"}'
    monkeypatch.setattr(c, "chat", fake_chat)
    s = Server(hostname="h", role="app", os="centos",
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    w = Wave(name="W", stage="3_application", server_ids=[s.id])
    r = c.assess_wave(w, [s], [], [])
    assert r["ok"] and r["go_no_go"] == "go"
    assert sent["system"] == "ASSESS MARKER"


def test_review_plan_uses_skill_file(tmp_path, monkeypatch):
    from idc.llm.client import LLMClient
    from idc.core.models import Server, Utilization, Wave, Workload
    _overlay_skill(tmp_path, monkeypatch, "review_plan", "REVIEW MARKER")
    sent = {}
    c = LLMClient(Settings(llm_enabled=True, skills_dir=str(tmp_path)))

    def fake_chat(messages, stream=False, timeout=None):
        sent["system"] = messages[0]["content"]
        return '{"overall":"sound","findings":[],"summary":"ok"}'
    monkeypatch.setattr(c, "chat", fake_chat)
    s = Server(hostname="h", role="app", os="centos", app_ids=["a"],
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    w = Wave(name="W", stage="3_application", server_ids=[s.id])
    r = c.review_plan([w], [s], [Workload(app_id="a", name="n", server_ids=[s.id])], [], [])
    assert r["ok"] and r["overall"] == "sound"
    assert sent["system"] == "REVIEW MARKER"


# ---------------------------------------------------------------------------
# directory-form skills (round 1): SKILL.md + references/ + scripts/
# ---------------------------------------------------------------------------
def _write_dir_skill(base: Path, name: str, body: str,
                     refs=None, scripts=None, backend="codex", mode="plan"):
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\nbackend: {backend}\n"
        + (f"mode: {mode}\n" if backend == "codex" else "")
        + "output: json\n---\n" + body, encoding="utf-8")
    if refs:
        rd = d / "references"
        rd.mkdir()
        for fn, txt in refs.items():
            (rd / fn).write_text(txt, encoding="utf-8")
    if scripts:
        sd = d / "scripts"
        sd.mkdir()
        for fn, txt in scripts.items():
            (sd / fn).write_text(txt, encoding="utf-8")
    return d


def test_directory_form_skill_loads_with_files(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "AUDIT THE CODE",
                     refs={"notes.md": "ref body"},
                     scripts={"run.py": "print('hi')"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    s = get_skill("code_audit", Settings(skills_dir=str(tmp_path)))
    assert s is not None and s.backend == "codex"
    assert s.system == "AUDIT THE CODE"
    assert s.dir and s.dir.endswith("code_audit")
    paths = {f.path: f for f in s.files}
    assert paths["references/notes.md"].kind == "reference"
    assert paths["references/notes.md"].size == len("ref body")
    assert paths["scripts/run.py"].kind == "script"
    assert paths["scripts/run.py"].size == len("print('hi')")


def test_single_file_form_is_not_loaded(tmp_path, monkeypatch):
    # the ONLY on-disk form is <name>/SKILL.md — a stray single-file <name>.md
    # is NOT picked up by the loader (no silent dir-vs-file precedence to know)
    (tmp_path / "plain.md").write_text(
        "---\nname: plain\nbackend: chat\noutput: json\n---\nX\n",
        encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    assert get_skill("plain", Settings(skills_dir=str(tmp_path))) is None


def test_read_skill_file_returns_content(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "X", refs={"notes.md": "hello"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import read_skill_file
    s = Settings(skills_dir=str(tmp_path))
    res = read_skill_file("code_audit", "references/notes.md", s)
    assert res is not None
    content, kind, size = res
    assert content == "hello" and kind == "reference" and size == 5


def test_read_skill_file_rejects_unlisted_and_traversal(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "X", refs={"notes.md": "hello"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import read_skill_file
    s = Settings(skills_dir=str(tmp_path))
    # the loader's file list is the allowlist — anything not bundled is None,
    # so path traversal (../) and unlisted paths can't reach anything
    assert read_skill_file("code_audit", "../etc/passwd", s) is None
    assert read_skill_file("code_audit", "references/missing.md", s) is None
    assert read_skill_file("code_audit", "scripts/none.py", s) is None
    assert read_skill_file("code_audit", "SKILL.md", s) is None


def test_read_skill_file_none_for_dir_skill_without_files(tmp_path, monkeypatch):
    # a dir-form skill with no bundled references/scripts → no bundled files
    d = tmp_path / "plain"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: plain\nbackend: codex\nmode: plan\noutput: json\n---\nX\n",
        encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import read_skill_file
    assert read_skill_file("plain", "references/x.md",
                           Settings(skills_dir=str(tmp_path))) is None


# ---------------------------------------------------------------------------
# runner — codex backend stages the skill's bundled files (round 1)
# ---------------------------------------------------------------------------
def test_codex_backend_stages_files_and_preamble(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "AUDIT THE CODE",
                     refs={"notes.md": "ref body"},
                     scripts={"run.py": "print('hi')"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc import agent
    from pathlib import Path
    captured = {}

    def fake(prompt, settings=None, **kw):
        captured.update(kw)
        captured["prompt"] = prompt
        # while the agent runs, the staged workspace must contain the files
        ws = kw.get("cwd")
        assert ws, "codex skill with files must run in a staged workspace"
        assert (Path(ws) / "references" / "notes.md").read_text() == "ref body"
        assert (Path(ws) / "scripts" / "run.py").read_text() == "print('hi')"
        return _FakeAgentResult('{"verdict":"change","confidence":0.9}')
    monkeypatch.setattr(agent, "run_agent_sync", fake)
    obj = run_skill("code_audit", "look at the repo",
                    settings=Settings(skills_dir=str(tmp_path)))
    assert obj["verdict"] == "change"
    assert captured["cwd"] and captured["add_dirs"] == [captured["cwd"]]
    assert captured["context"].startswith("--- SKILL RESOURCES ---")
    assert "references/notes.md" in captured["context"]
    assert "scripts/run.py" in captured["context"]
    # the system prompt body still follows the preamble
    assert "AUDIT THE CODE" in captured["context"]


def test_codex_backend_no_files_skips_workspace(tmp_path, monkeypatch):
    # codex skill with no bundled files → no cwd/add_dirs, plain context
    d = tmp_path / "code_plain"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: code_plain\ndescription: x\nbackend: codex\nmode: plan\n"
        "output: json\n---\nPLAIN\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc import agent
    captured = {}
    monkeypatch.setattr(agent, "run_agent_sync",
                        lambda prompt, settings=None, **kw:
                        (captured.update(kw) or
                         captured.update({"prompt": prompt}) or
                         _FakeAgentResult('{"x":1}')))
    run_skill("code_plain", "hi", settings=Settings(skills_dir=str(tmp_path)))
    assert captured.get("cwd") is None and captured.get("add_dirs") is None
    assert captured["context"] == "PLAIN"  # no preamble prepended


def test_chat_backend_ignores_skill_files(tmp_path, monkeypatch):
    # a chat-backend skill with bundled files: the chat path has no tool use,
    # so files are NOT inlined into the system prompt and no preamble is added
    _write_dir_skill(tmp_path, "chatskill", "MY SYSTEM PROMPT",
                     refs={"notes.md": "ref body"}, backend="chat")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()

    class _Cap:
        def __init__(self, reply):
            self.reply = reply
            self.sent_system = None
        def chat(self, messages, stream=False, timeout=None):
            self.sent_system = messages[0]["content"]
            return self.reply
    llm = _Cap('{"x":1}')
    obj = run_skill("chatskill", "ctx", output="json",
                    settings=Settings(skills_dir=str(tmp_path)), llm=llm)
    assert obj == {"x": 1}
    # system prompt sent to chat == the body verbatim (files are codex-only)
    assert llm.sent_system == "MY SYSTEM PROMPT"
    assert "SKILL RESOURCES" not in llm.sent_system


# ---------------------------------------------------------------------------
# round 2: write/delete bundled files (UI editor) + dir-form write_skill
# ---------------------------------------------------------------------------
def test_write_skill_file_materializes_new_codex_skill(tmp_path, monkeypatch):
    # no overlay yet — adding a file to a brand-new skill materializes a codex
    # stub SKILL.md + the bundled file (files only apply to codex skills)
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file, get_skill
    s = Settings(skills_dir=str(tmp_path))
    p = write_skill_file("mycodex", "scripts/run.py", "print(1)", s)
    assert p.endswith("mycodex/scripts/run.py")
    assert (tmp_path / "mycodex" / "SKILL.md").is_file()  # codex stub materialized
    s2 = get_skill("mycodex", s)
    assert s2.dir and s2.backend == "codex"
    assert any(f.path == "scripts/run.py" and f.kind == "script" for f in s2.files)


def test_write_skill_file_preserves_existing_codex_body(tmp_path, monkeypatch):
    # a saved codex skill keeps its SKILL.md body when a bundled file is added
    # (the file is written under <name>/, the SKILL.md is untouched)
    d = tmp_path / "code_audit"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: code_audit\nbackend: codex\nmode: plan\noutput: json\n"
        "---\nMY EDITED BODY\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file, get_skill
    s = Settings(skills_dir=str(tmp_path))
    p = write_skill_file("code_audit", "references/notes.md", "ref body", s)
    assert p.endswith("code_audit/references/notes.md")
    assert (tmp_path / "code_audit" / "SKILL.md").read_text() == (
        "---\nname: code_audit\nbackend: codex\nmode: plan\noutput: json\n"
        "---\nMY EDITED BODY\n")
    s2 = get_skill("code_audit", s)
    assert s2.dir and s2.system == "MY EDITED BODY"
    assert any(f.path == "references/notes.md" and f.kind == "reference"
               for f in s2.files)


def test_write_skill_file_switches_chat_skill_to_codex(tmp_path, monkeypatch):
    # chat-backend skills ignore bundled files (no tool use), so attaching one
    # SWITCHES the skill to codex first (materialize an overlay SKILL.md with
    # backend: codex, body preserved) instead of rejecting. seven_r ships chat.
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file, get_skill
    s = Settings(skills_dir=str(tmp_path))
    p = write_skill_file("seven_r", "references/notes.md", "ref body", s)
    assert p.endswith("seven_r/references/notes.md")
    # an overlay SKILL.md is materialized and flipped to codex
    sk_md = tmp_path / "seven_r" / "SKILL.md"
    assert sk_md.is_file()
    flipped = sk_md.read_text(encoding="utf-8")
    assert re.search(r"(?m)^backend:\s*codex\s*$", flipped)
    assert "cloud-migration strategist" in flipped  # original body preserved
    # the bundled file is listed and the skill is now codex-backed
    s2 = get_skill("seven_r", s)
    assert s2.backend == "codex"
    assert s2.dir and str(s2.dir).endswith("seven_r")
    assert any(f.path == "references/notes.md" and f.kind == "reference"
               for f in s2.files)


def test_write_skill_file_flips_existing_chat_overlay_to_codex(tmp_path, monkeypatch):
    # an operator's overlay seven_r/SKILL.md says backend: chat — attaching a
    # bundled file flips it to codex IN PLACE (body + every other frontmatter
    # field preserved, only the backend line changes + mode: plan is added).
    d = tmp_path / "seven_r"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: seven_r\ndescription: custom\nbackend: chat\noutput: json\n"
        "max_rounds: 2\n---\nMY CUSTOM BODY\n", encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file, get_skill
    s = Settings(skills_dir=str(tmp_path))
    write_skill_file("seven_r", "scripts/run.py", "print(1)\n", s)
    flipped = (tmp_path / "seven_r" / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"(?m)^backend:\s*codex\s*$", flipped)
    assert re.search(r"(?m)^mode:\s*plan\s*$", flipped)
    # everything else the operator wrote is preserved verbatim
    assert "description: custom" in flipped
    assert "max_rounds: 2" in flipped
    assert "MY CUSTOM BODY" in flipped
    s2 = get_skill("seven_r", s)
    assert s2.backend == "codex" and s2.system == "MY CUSTOM BODY"
    assert s2.max_rounds == 2  # preserved
    assert any(f.path == "scripts/run.py" and f.kind == "script" for f in s2.files)


def test_write_skill_file_compiles_script(tmp_path, monkeypatch):
    # a syntactically valid script is accepted
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file
    s = Settings(skills_dir=str(tmp_path))
    p = write_skill_file("mycodex", "scripts/run.py", "x = 1\nprint(x)\n", s)
    assert p.endswith("mycodex/scripts/run.py")


def test_write_skill_file_rejects_script_that_does_not_compile(tmp_path, monkeypatch):
    # a broken script is rejected BEFORE it's written (so it can't blow up only
    # when the codex agent tries to run it)
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file
    s = Settings(skills_dir=str(tmp_path))
    with pytest.raises(ValueError) as ei:
        write_skill_file("mycodex", "scripts/run.py", "def broken(:\n", s)
    assert "does not compile" in str(ei.value)
    assert not (tmp_path / "mycodex" / "scripts" / "run.py").exists()


def test_write_skill_file_rejects_bad_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill_file
    s = Settings(skills_dir=str(tmp_path))
    for bad in ["../etc/passwd", "references/../x.md", "references/nested/x.md",
                "other/foo.md", "scripts/run.txt", "references/", ""]:
        with pytest.raises(ValueError):
            write_skill_file("seven_r", bad, "x", s)


def test_write_skill_file_requires_overlay_dir():
    from idc.llm.skills import write_skill_file
    with pytest.raises(ValueError):
        write_skill_file("seven_r", "references/x.md", "x",
                         Settings(skills_dir=""))


def test_delete_skill_file_removes_bundled_file(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "X",
                     refs={"notes.md": "ref"}, scripts={"run.py": "print(1)"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import delete_skill_file, get_skill
    s = Settings(skills_dir=str(tmp_path))
    assert delete_skill_file("code_audit", "references/notes.md", s) is True
    assert not (tmp_path / "code_audit" / "references" / "notes.md").exists()
    s2 = get_skill("code_audit", s)
    assert not any(f.path == "references/notes.md" for f in s2.files)
    assert any(f.path == "scripts/run.py" for f in s2.files)  # other file intact


def test_delete_skill_file_rejects_unlisted(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "X", refs={"notes.md": "ref"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import delete_skill_file
    s = Settings(skills_dir=str(tmp_path))
    # allowlist — can't delete a path that isn't bundled (no traversal)
    assert delete_skill_file("code_audit", "../etc/passwd", s) is False
    assert delete_skill_file("code_audit", "references/missing.md", s) is False
    assert delete_skill_file("code_audit", "SKILL.md", s) is False


def test_write_skill_writes_skillmd_when_dir_form(tmp_path, monkeypatch):
    # once a skill is directory-form in the overlay, Save (write_skill) must
    # write <name>/SKILL.md — a single-file <name>.md would be shadowed and lost
    _write_dir_skill(tmp_path, "code_audit", "OLD BODY",
                     refs={"notes.md": "ref"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill, get_skill
    s = Settings(skills_dir=str(tmp_path))
    md = ("---\nname: code_audit\nbackend: codex\nmode: plan\noutput: json\n"
          "---\nNEW BODY\n")
    p = write_skill("code_audit", md, s)
    assert p.endswith("code_audit/SKILL.md")
    assert not (tmp_path / "code_audit.md").exists()  # no stray single-file
    s2 = get_skill("code_audit", s)
    assert s2.system == "NEW BODY"
    assert any(f.path == "references/notes.md" for f in s2.files)  # bundle kept


# ---------------------------------------------------------------------------
# round 3: one-step undo (.prev snapshot) + try_skill (preview before save)
# ---------------------------------------------------------------------------
def test_write_skill_snapshots_prev(tmp_path, monkeypatch):
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill, get_skill_prev
    s = Settings(skills_dir=str(tmp_path))
    write_skill("foo", "---\nname: foo\nbackend: chat\noutput: json\n---\nV1\n", s)
    write_skill("foo", "---\nname: foo\nbackend: chat\noutput: json\n---\nV2\n", s)
    # the previous overlay content is snapshotted next to the live file
    assert get_skill_prev("foo", s) == (
        "---\nname: foo\nbackend: chat\noutput: json\n---\nV1\n")


def test_undo_skill_restores_prev(tmp_path, monkeypatch):
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill, undo_skill, get_skill, get_skill_prev
    s = Settings(skills_dir=str(tmp_path))
    write_skill("foo", "---\nname: foo\nbackend: chat\noutput: json\n---\nV1\n", s)
    write_skill("foo", "---\nname: foo\nbackend: chat\noutput: json\n---\nV2\n", s)
    assert get_skill("foo", s).system == "V2"
    md = undo_skill("foo", s)
    assert md is not None and "V1" in md
    assert get_skill("foo", s).system == "V1"   # restored
    assert get_skill_prev("foo", s) is None     # snapshot consumed (one-shot)


def test_undo_skill_none_without_overlay():
    from idc.llm.skills import undo_skill, get_skill_prev
    s = Settings(skills_dir="")
    # builtin/fallback skills have no overlay → nothing to undo
    assert undo_skill("seven_r", s) is None
    assert get_skill_prev("seven_r", s) is None


def test_undo_dir_form_skill(tmp_path, monkeypatch):
    # dir-form skills: write_skill snapshots SKILL.md.prev, undo restores it
    _write_dir_skill(tmp_path, "code_audit", "V1",
                     refs={"notes.md": "ref"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.skills import write_skill, undo_skill, get_skill
    s = Settings(skills_dir=str(tmp_path))
    write_skill("code_audit",
                "---\nname: code_audit\nbackend: codex\nmode: plan\n"
                "output: json\n---\nV2\n", s)
    undo_skill("code_audit", s)
    assert get_skill("code_audit", s).system == "V1"
    assert any(f.path == "references/notes.md" for f in get_skill("code_audit", s).files)


def test_try_skill_chat_json(tmp_path, monkeypatch):
    d = tmp_path / "mychat"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: mychat\nbackend: chat\noutput: json\n---\nSYS\n",
        encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.runner import try_skill
    llm = _FakeLLM(['{"x":1}'])
    r = try_skill("mychat", "hello", Settings(skills_dir=str(tmp_path)), llm=llm)
    assert r["ok"] and r["kind"] == "json"
    assert r["output"] == {"x": 1}
    assert llm.calls == 1


def test_try_skill_chat_text(tmp_path, monkeypatch):
    d = tmp_path / "mytext"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: mytext\nbackend: chat\noutput: text\n---\nSYS\n",
        encoding="utf-8")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.runner import try_skill
    llm = _FakeLLM(["free-form reply"])
    r = try_skill("mytext", "hi", Settings(skills_dir=str(tmp_path)), llm=llm)
    assert r["ok"] and r["kind"] == "text" and r["output"] == "free-form reply"


def test_try_skill_markdown_override_uses_transient_body(tmp_path, monkeypatch):
    # try an UNSAVED edit: markdown= overrides the resolved skill's system
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.runner import try_skill

    class _Cap:
        def __init__(self, reply): self.reply = reply; self.sent = None
        def chat(self, messages, stream=False, timeout=None):
            self.sent = messages[0]["content"]; return self.reply
    llm = _Cap('{"x":1}')
    md = "---\nname: mychat\nbackend: chat\noutput: json\n---\nTRANSIENT BODY\n"
    r = try_skill("mychat", "hi", Settings(skills_dir=str(tmp_path)),
                  markdown=md, llm=llm)
    assert r["ok"] and r["output"] == {"x": 1}
    assert llm.sent == "TRANSIENT BODY"   # the override was used, not a saved skill


def test_try_skill_codex_resources_touched(tmp_path, monkeypatch):
    _write_dir_skill(tmp_path, "code_audit", "AUDIT",
                     refs={"notes.md": "ref"}, scripts={"run.py": "print(1)"})
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc import agent
    from types import SimpleNamespace
    events = [
        SimpleNamespace(kind="tool", text="[tool] cat references/notes.md"),
        SimpleNamespace(kind="tool", text="[tool] python scripts/run.py"),
        SimpleNamespace(kind="text", text='{"verdict":"ok"}'),
    ]
    monkeypatch.setattr(agent, "run_agent_sync",
                        lambda prompt, settings=None, **kw: SimpleNamespace(
                            output='{"verdict":"ok"}', status="done",
                            error="", events=events))
    from idc.llm.runner import try_skill
    r = try_skill("code_audit", "look at the repo",
                  Settings(skills_dir=str(tmp_path)))
    assert r["ok"] and r["kind"] == "codex"
    assert r["output"] == '{"verdict":"ok"}'
    # bundled files the agent referenced in tool events → "used in last run"
    assert "references/notes.md" in r["resources_touched"]
    assert "scripts/run.py" in r["resources_touched"]


def test_try_skill_wired_rejects():
    # seven_r takes a host/app — Try points to the drawer, not free text
    from idc.llm.runner import try_skill
    r = try_skill("seven_r", "hi", Settings(skills_dir=""))
    assert r["ok"] is False and r["kind"] == "wired"
    assert "drawer" in r["error"]


def test_try_skill_not_found():
    from idc.llm.runner import try_skill
    r = try_skill("totally_unknown", "hi", Settings(skills_dir=""))
    assert r["ok"] is False and r["kind"] == "not_found"


def test_try_skill_bad_markdown():
    from idc.llm.runner import try_skill
    r = try_skill("x", "hi", Settings(skills_dir=""), markdown="not markdown")
    assert r["ok"] is False and r["kind"] == "bad_markdown"


# ---------------------------------------------------------------------------
# round 4: run_skill_script — run a skill's bundled python (the editor's
# current content) in a sandbox; the Skills-page Run button.
# ---------------------------------------------------------------------------
def _codex_skill_with_scripts(tmp_path, monkeypatch, name="code_audit",
                              scripts=None, refs=None):
    _write_dir_skill(tmp_path, name, "AUDIT THE CODE",
                     refs=refs, scripts=scripts, backend="codex", mode="plan")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    return Settings(skills_dir=str(tmp_path))


def test_run_skill_script_ok(tmp_path, monkeypatch):
    s = _codex_skill_with_scripts(tmp_path, monkeypatch,
                                   scripts={"run.py": "print('hi')\n"})
    from idc.llm.runner import run_skill_script
    r = run_skill_script("code_audit", "scripts/run.py", "print('hi')\n", s)
    assert r["ok"] is True and r["exit_code"] == 0
    assert r["stdout"] == "hi\n" and r["stderr"] == ""
    assert r["duration_ms"] >= 0


def test_run_skill_script_uses_editor_content_not_saved(tmp_path, monkeypatch):
    # saved run.py prints 'SAVED', but the editor content prints 'EDITED' —
    # the run uses the editor's current (unsaved) value
    s = _codex_skill_with_scripts(tmp_path, monkeypatch,
                                   scripts={"run.py": "print('SAVED')\n"})
    from idc.llm.runner import run_skill_script
    r = run_skill_script("code_audit", "scripts/run.py", "print('EDITED')\n", s)
    assert r["ok"] is True and r["stdout"] == "EDITED\n"


def test_run_skill_script_nonzero_exit_and_stderr(tmp_path, monkeypatch):
    s = _codex_skill_with_scripts(tmp_path, monkeypatch, scripts={"run.py": "x"})
    from idc.llm.runner import run_skill_script
    code = "import sys\nsys.stderr.write('boom\\n')\nsys.exit(2)\n"
    r = run_skill_script("code_audit", "scripts/run.py", code, s)
    assert r["ok"] is True and r["exit_code"] == 2
    assert "boom" in r["stderr"]


def test_run_skill_script_stages_sibling_files(tmp_path, monkeypatch):
    # the script reads references/notes.md — the runner stages the skill's
    # bundled references alongside so the script can open them
    s = _codex_skill_with_scripts(tmp_path, monkeypatch,
                                   refs={"notes.md": "REFBODY"},
                                   scripts={"run.py": "x"})
    from idc.llm.runner import run_skill_script
    code = "print(open('references/notes.md').read())\n"
    r = run_skill_script("code_audit", "scripts/run.py", code, s)
    assert r["ok"] is True and r["stdout"] == "REFBODY\n"


def test_run_skill_script_switches_chat_skill_to_codex(tmp_path, monkeypatch):
    # seven_r ships as chat — running a script SWITCHES it to codex first
    # (materializes an overlay SKILL.md with backend: codex, body preserved)
    # instead of rejecting, then runs the script.
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    reset_registry()
    from idc.llm.runner import run_skill_script
    from idc.llm.skills import get_skill
    s = Settings(skills_dir=str(tmp_path))
    r = run_skill_script("seven_r", "scripts/run.py", "print(1)\n", s)
    assert r["ok"] is True and r["exit_code"] == 0
    assert r["stdout"] == "1\n"
    # the skill is now codex (overlay materialized + flipped, body preserved)
    sk_md = tmp_path / "seven_r" / "SKILL.md"
    assert sk_md.is_file()
    import re
    assert re.search(r"(?m)^backend:\s*codex\s*$",
                     sk_md.read_text(encoding="utf-8"))
    assert "cloud-migration strategist" in sk_md.read_text(encoding="utf-8")
    s2 = get_skill("seven_r", s)
    assert s2.backend == "codex"


def test_run_skill_script_chat_skill_without_overlay_dir_errors(tmp_path, monkeypatch):
    # no overlay dir configured → can't switch chat→codex → clear error (not a
    # silent no-op, not a crash)
    monkeypatch.delenv("IDC_SKILLS_DIR", raising=False)
    reset_registry()
    from idc.llm.runner import run_skill_script
    r = run_skill_script("seven_r", "scripts/run.py", "print(1)\n",
                         Settings(skills_dir=""))
    assert r["ok"] is False
    assert "codex" in r["error"] and "IDC_SKILLS_DIR" in r["error"]


def test_run_skill_script_rejects_non_script_path(tmp_path, monkeypatch):
    s = _codex_skill_with_scripts(tmp_path, monkeypatch, scripts={"run.py": "x"})
    from idc.llm.runner import run_skill_script
    for bad in ["references/notes.md", "scripts/run.txt", "scripts/run", "other/x.py"]:
        r = run_skill_script("code_audit", bad, "x", s)
        assert r["ok"] is False and "scripts/<name>.py" in r["error"], bad


def test_run_skill_script_no_inherited_secrets(tmp_path, monkeypatch):
    # the child env is minimal — backend secrets (IDC_DB_URL etc.) must NOT
    # reach the script even if they're in the backend process env
    monkeypatch.setenv("IDC_DB_URL", "mysql://supersecret:pw@db/idc")
    monkeypatch.setenv("IDC_SKILLS_DIR", str(tmp_path))
    s = _codex_skill_with_scripts(tmp_path, monkeypatch, scripts={"run.py": "x"})
    from idc.llm.runner import run_skill_script
    code = "import os\nprint(os.environ.get('IDC_DB_URL','NONE'))\n"
    r = run_skill_script("code_audit", "scripts/run.py", code, s)
    assert r["ok"] is True
    assert r["stdout"] == "NONE\n", "backend secret leaked into the child"


def test_run_skill_script_timeout(tmp_path, monkeypatch):
    s = _codex_skill_with_scripts(tmp_path, monkeypatch, scripts={"run.py": "x"})
    from idc.llm import runner
    monkeypatch.setattr(runner, "_RUN_TIMEOUT_S", 1)   # 1s for the test
    code = "import time\ntime.sleep(2)\n"
    r = runner.run_skill_script("code_audit", "scripts/run.py", code, s)
    assert r["ok"] is False and "timeout" in r["error"]