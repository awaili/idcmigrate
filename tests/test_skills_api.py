"""Tests for the /api/skills management endpoints (list / get / put / delete)
+ the auth gate (operator session required when web_password is set).

Edits write to the IDC_SKILLS_DIR overlay; the loader re-scans on write so the
change takes effect immediately (verified by run_skill picking up the overlay
body). No restart.
"""
import os

import pytest

pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient
import idc.backend.app as m
from idc.config import reset_settings
from idc.llm import skills as S

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _clean_skills_env():
    """No overlay dir + fresh settings + fresh skill registry per test, so
    IDC_SKILLS_DIR changes + overlay writes don't leak across tests."""
    os.environ.pop("IDC_SKILLS_DIR", None)
    reset_settings()
    S.reset_registry()
    yield
    os.environ.pop("IDC_SKILLS_DIR", None)
    reset_settings()
    S.reset_registry()


def _overlay(tmp_path):
    os.environ["IDC_SKILLS_DIR"] = str(tmp_path)
    reset_settings()
    S.reset_registry()


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------
def test_list_skills_returns_seven_with_source_builtin():
    r = client.get("/api/skills")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "seven_r" in names and "audit_match" in names
    seven = next(s for s in r.json() if s["name"] == "seven_r")
    assert seven["source"] == "builtin" and seven["backend"] == "chat"
    assert seven["editable"] is False        # no overlay dir configured
    assert seven["overriding_default"] is True
    assert seven["used_by"], "used_by hint missing — operator can't tell what seven_r drives"


def test_list_skills_fallback_source_for_constant_only_skill():
    r = client.get("/api/skills")
    # lz_design has a registered _FALLBACK but no shipped skill file → source
    # is "fallback" (synthesized from the constant). (The 4 wired skills —
    # seven_r/audit_match/assess_wave/review_plan — ship files, so they're
    # "builtin"; the loop-driven ones — lz_design/network_policy/waveplan —
    # are fallback-only until they ship a file.)
    lz = next(s for s in r.json() if s["name"] == "lz_design")
    assert lz["source"] == "fallback"


def test_get_skill_has_markdown_and_body():
    r = client.get("/api/skills/seven_r")
    assert r.status_code == 200
    j = r.json()
    assert "markdown" in j and j["system"] in j["markdown"]
    assert j["source"] == "builtin" and j["overriding_default"] is True
    assert j["default_body"]                    # the shipped SKILL.md body (diff/reset target)


def test_get_skill_missing_404():
    assert client.get("/api/skills/nope").status_code == 404


# ---------------------------------------------------------------------------
# directory-form skills (round 1) — bundled reference/script files
# ---------------------------------------------------------------------------
def _overlay_dir_skill(tmp_path, name="code_audit"):
    """Write a directory-form skill into the overlay: <name>/SKILL.md +
    references/notes.md + scripts/run.py, then point IDC_SKILLS_DIR at it."""
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\nbackend: codex\nmode: plan\n"
        f"output: json\n---\nAUDIT THE CODE\n", encoding="utf-8")
    (d / "references").mkdir()
    (d / "references" / "notes.md").write_text("hello", encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    _overlay(tmp_path)


def test_get_skill_includes_files_for_directory_form(tmp_path):
    _overlay_dir_skill(tmp_path)
    j = client.get("/api/skills/code_audit").json()
    paths = sorted(f["path"] for f in j["files"])
    assert paths == ["references/notes.md", "scripts/run.py"]
    kinds = {f["path"]: f["kind"] for f in j["files"]}
    assert kinds["references/notes.md"] == "reference"
    assert kinds["scripts/run.py"] == "script"
    assert j["dir"] and j["dir"].endswith("code_audit")


def test_list_skills_includes_files_for_directory_form(tmp_path):
    _overlay_dir_skill(tmp_path)
    rows = client.get("/api/skills").json()
    row = next(r for r in rows if r["name"] == "code_audit")
    assert any(f["path"] == "references/notes.md" for f in row["files"])


def test_get_skill_file_content(tmp_path):
    _overlay_dir_skill(tmp_path)
    r = client.get("/api/skills/code_audit/files/references/notes.md")
    assert r.status_code == 200
    j = r.json()
    assert j["content"] == "hello" and j["kind"] == "reference"
    assert j["size"] == 5 and j["path"] == "references/notes.md"
    # a script file too
    r2 = client.get("/api/skills/code_audit/files/scripts/run.py")
    assert r2.status_code == 200 and r2.json()["kind"] == "script"


def test_get_skill_file_404_for_unlisted_path(tmp_path):
    _overlay_dir_skill(tmp_path)
    # the loader's file list is the allowlist — unlisted paths 404 (no traversal)
    assert client.get("/api/skills/code_audit/files/references/missing.md"
                      ).status_code == 404
    assert client.get("/api/skills/code_audit/files/scripts/none.py"
                      ).status_code == 404


def test_get_skill_file_404_for_skill_with_no_bundled_files(tmp_path):
    _overlay(tmp_path)
    client.put("/api/skills/seven_r", json={
        "markdown": "---\nname: seven_r\nbackend: chat\noutput: json\n---\nX\n"})
    # the overlay seven_r/SKILL.md has no bundled files → any file path 404
    assert client.get("/api/skills/seven_r/files/references/x.md"
                      ).status_code == 404


def test_delete_skill_removes_directory_form(tmp_path):
    _overlay_dir_skill(tmp_path, "mydir")
    assert client.get("/api/skills/mydir").status_code == 200
    r = client.delete("/api/skills/mydir")
    assert r.status_code == 200 and r.json()["removed"] is True
    # the whole skill dir is gone → falls back / 404 (mydir has no builtin/fallback)
    assert client.get("/api/skills/mydir").status_code == 404


# ---------------------------------------------------------------------------
# round 2: PUT / DELETE bundled files (UI editor)
# ---------------------------------------------------------------------------
def test_put_skill_file_creates_in_dir_form(tmp_path):
    _overlay_dir_skill(tmp_path)
    r = client.put("/api/skills/code_audit/files/references/new.md",
                   json={"content": "new ref"})
    assert r.status_code == 200 and r.json()["saved"] is True
    # the file is now listed on the skill
    j = client.get("/api/skills/code_audit").json()
    assert any(f["path"] == "references/new.md" for f in j["files"])
    # and readable
    assert client.get("/api/skills/code_audit/files/references/new.md"
                      ).json()["content"] == "new ref"


def test_put_skill_file_on_codex_skill_preserves_body(tmp_path):
    # save a codex skill, then add a bundled file → the SKILL.md body is
    # preserved and the file is listed
    _overlay(tmp_path)
    client.put("/api/skills/mycodex", json={
        "markdown": "---\nname: mycodex\nbackend: codex\nmode: plan\n"
                    "output: json\n---\nEDITED BODY\n"})
    r = client.put("/api/skills/mycodex/files/references/notes.md",
                   json={"content": "ref"})
    assert r.status_code == 200
    j = client.get("/api/skills/mycodex").json()
    assert j["system"] == "EDITED BODY"  # body preserved
    assert j["source"] == "overlay"
    assert any(f["path"] == "references/notes.md" for f in j["files"])


def test_put_skill_file_on_chat_skill_switches_to_codex(tmp_path):
    # chat-backend skills ignore bundled files, so the API SWITCHES the skill to
    # codex first (materializes an overlay SKILL.md with backend: codex) rather
    # than rejecting. seven_r ships as chat.
    _overlay(tmp_path)
    r = client.put("/api/skills/seven_r/files/references/notes.md",
                   json={"content": "ref"})
    assert r.status_code == 200
    j = client.get("/api/skills/seven_r").json()
    assert j["backend"] == "codex"           # flipped
    assert j["source"] == "overlay"           # overlay materialized
    assert any(f["path"] == "references/notes.md" for f in j["files"])


def test_put_skill_file_broken_script_400(tmp_path):
    # a script that doesn't compile is rejected before it's written
    _overlay(tmp_path)
    r = client.put("/api/skills/mycodex/files/scripts/run.py",
                   json={"content": "def broken(:\n"})
    assert r.status_code == 400
    assert "does not compile" in r.json()["detail"]


def test_put_skill_file_rejects_bad_path(tmp_path):
    _overlay_dir_skill(tmp_path)
    # these reach the route and are rejected by the path validator (traversal
    # is also blocked at the URL layer — TestClient normalizes ../ away to a
    # 404 before the route; the loader-level test covers the literal ../ case)
    for bad in ["other/foo.md", "scripts/run.txt", "references/"]:
        r = client.put("/api/skills/code_audit/files/"+bad, json={"content": "x"})
        assert r.status_code == 400, f"{bad} should be rejected"
    # scripts must be .py
    assert client.put("/api/skills/code_audit/files/scripts/run.sh",
                      json={"content": "x"}).status_code == 400


# ---------------------------------------------------------------------------
# round 4: POST /api/skills/{name}/run-script — run a bundled script online
# ---------------------------------------------------------------------------
def test_run_script_endpoint_runs(tmp_path):
    _overlay_dir_skill(tmp_path)  # code_audit codex skill with scripts/run.py
    r = client.post("/api/skills/code_audit/run-script", json={
        "path": "scripts/run.py", "content": "print('online')\n"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["exit_code"] == 0
    assert j["stdout"] == "online\n"


def test_run_script_endpoint_switches_chat_skill_to_codex(tmp_path):
    # chat-backend skills: running a script switches the skill to codex first
    # (flips the overlay SKILL.md in place) instead of rejecting.
    _overlay(tmp_path)
    client.put("/api/skills/seven_r", json={
        "markdown": "---\nname: seven_r\nbackend: chat\noutput: json\n---\nX\n"})
    r = client.post("/api/skills/seven_r/run-script", json={
        "path": "scripts/run.py", "content": "print(1)\n"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["exit_code"] == 0 and j["stdout"] == "1\n"
    # the skill is now codex
    assert client.get("/api/skills/seven_r").json()["backend"] == "codex"


def test_run_script_endpoint_rejects_missing_path(tmp_path):
    _overlay_dir_skill(tmp_path)
    assert client.post("/api/skills/code_audit/run-script",
                       json={"content": "print(1)\n"}).status_code == 400


def test_run_script_endpoint_requires_session(_auth_on):
    # operator-only — a leaked executor bearer must NOT run code on the box
    assert client.post("/api/skills/seven_r/run-script",
                       json={"path": "scripts/run.py", "content": "print(1)\n"}
                       ).status_code == 401
    h = {"Authorization": "Bearer exec-tok"}
    assert client.post("/api/skills/seven_r/run-script", json={
        "path": "scripts/run.py", "content": "print(1)\n"},
                       headers=h).status_code == 401


def test_put_skill_file_without_overlay_dir_400():
    r = client.put("/api/skills/seven_r/files/references/x.md",
                   json={"content": "x"})
    assert r.status_code == 400 and "IDC_SKILLS_DIR" in r.json()["detail"]


def test_delete_skill_file(tmp_path):
    _overlay_dir_skill(tmp_path)
    r = client.delete("/api/skills/code_audit/files/references/notes.md")
    assert r.status_code == 200 and r.json()["removed"] is True
    j = client.get("/api/skills/code_audit").json()
    assert not any(f["path"] == "references/notes.md" for f in j["files"])
    # the other file (scripts/run.py) is intact
    assert any(f["path"] == "scripts/run.py" for f in j["files"])


def test_delete_skill_file_unlisted_returns_false(tmp_path):
    _overlay_dir_skill(tmp_path)
    r = client.delete("/api/skills/code_audit/files/references/missing.md")
    assert r.status_code == 200 and r.json()["removed"] is False


def test_skill_file_endpoints_require_session(_auth_on):
    # auth on, no session → 401 (operator-only; not in bearer list)
    assert client.put("/api/skills/seven_r/files/references/x.md",
                      json={"content": "x"}).status_code == 401
    assert client.delete("/api/skills/seven_r/files/references/x.md"
                         ).status_code == 401
    assert client.get("/api/skills/seven_r/files/references/x.md"
                      ).status_code == 401


def test_skill_file_endpoints_bearer_not_honored(_auth_on):
    """A bearer token (executor credential) must NOT unlock skill-file editing."""
    h = {"Authorization": "Bearer exec-tok"}
    assert client.put("/api/skills/seven_r/files/references/x.md", json={
        "content": "x"}, headers=h).status_code == 401
    assert client.delete("/api/skills/seven_r/files/references/x.md",
                         headers=h).status_code == 401


# ---------------------------------------------------------------------------
# round 3: /try (preview before save) + /prev + /undo (one-step revert)
# ---------------------------------------------------------------------------
def test_try_endpoint_forwards_input_and_markdown(tmp_path, monkeypatch):
    _overlay(tmp_path)
    import idc.llm.runner as runner
    captured = {}
    def fake(name, inp, settings, *, markdown=None):
        captured.update(name=name, inp=inp, markdown=markdown)
        return {"ok": True, "kind": "json", "output": {"x": 1},
                "error": "", "resources_touched": ["references/a.md"]}
    monkeypatch.setattr(runner, "try_skill", fake)
    r = client.post("/api/skills/mychat/try", json={
        "input": "hello", "markdown": "---\nname: mychat\nbackend: chat\noutput: json\n---\nX\n"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["output"] == {"x": 1}
    assert j["resources_touched"] == ["references/a.md"]
    assert captured["name"] == "mychat" and captured["inp"] == "hello"
    assert captured["markdown"] and "X" in captured["markdown"]   # override forwarded


def test_try_endpoint_no_markdown_passes_none(tmp_path, monkeypatch):
    _overlay(tmp_path)
    import idc.llm.runner as runner
    captured = {}
    def fake(name, inp, settings, *, markdown=None):
        captured["markdown"] = markdown
        return {"ok": True, "kind": "json", "output": {}, "error": "",
                "resources_touched": []}
    monkeypatch.setattr(runner, "try_skill", fake)
    client.post("/api/skills/mychat/try", json={"input": "hi"})
    assert captured["markdown"] is None


def test_get_prev_none_without_overlay():
    r = client.get("/api/skills/seven_r/prev")
    assert r.status_code == 200 and r.json()["has_prev"] is False
    assert r.json()["markdown"] == ""


def test_prev_after_two_saves(tmp_path):
    _overlay(tmp_path)
    client.put("/api/skills/mychat", json={
        "markdown": "---\nname: mychat\nbackend: chat\noutput: json\n---\nV1\n"})
    client.put("/api/skills/mychat", json={
        "markdown": "---\nname: mychat\nbackend: chat\noutput: json\n---\nV2\n"})
    r = client.get("/api/skills/mychat/prev")
    assert r.status_code == 200 and r.json()["has_prev"] is True
    assert "V1" in r.json()["markdown"]


def test_undo_endpoint_restores(tmp_path):
    _overlay(tmp_path)
    client.put("/api/skills/mychat", json={
        "markdown": "---\nname: mychat\nbackend: chat\noutput: json\n---\nV1\n"})
    client.put("/api/skills/mychat", json={
        "markdown": "---\nname: mychat\nbackend: chat\noutput: json\n---\nV2\n"})
    assert client.get("/api/skills/mychat").json()["system"] == "V2"
    r = client.post("/api/skills/mychat/undo")
    assert r.status_code == 200 and r.json()["restored"] is True
    assert "V1" in r.json()["markdown"]
    assert client.get("/api/skills/mychat").json()["system"] == "V1"
    # snapshot consumed → second undo is a no-op
    r2 = client.post("/api/skills/mychat/undo")
    assert r2.json()["restored"] is False


def test_undo_endpoint_no_prev_returns_false(tmp_path):
    _overlay(tmp_path)
    client.put("/api/skills/mychat", json={
        "markdown": "---\nname: mychat\nbackend: chat\noutput: json\n---\nV1\n"})
    # only one save → no .prev snapshot yet
    r = client.post("/api/skills/mychat/undo")
    assert r.status_code == 200 and r.json()["restored"] is False


def test_try_prev_undo_require_session(_auth_on):
    # auth on, no session → 401 (operator-only; not in the bearer list)
    assert client.post("/api/skills/seven_r/try",
                       json={"input": "x"}).status_code == 401
    assert client.get("/api/skills/seven_r/prev").status_code == 401
    assert client.post("/api/skills/seven_r/undo").status_code == 401


def test_try_prev_undo_bearer_not_honored(_auth_on):
    """A bearer token (executor credential) must NOT unlock try/undo."""
    h = {"Authorization": "Bearer exec-tok"}
    assert client.post("/api/skills/seven_r/try", json={
        "input": "x"}, headers=h).status_code == 401
    assert client.get("/api/skills/seven_r/prev", headers=h).status_code == 401
    assert client.post("/api/skills/seven_r/undo", headers=h).status_code == 401


# ---------------------------------------------------------------------------
# put / delete
# ---------------------------------------------------------------------------
def test_put_without_overlay_dir_400():
    r = client.put("/api/skills/seven_r", json={
        "markdown": "---\nname: seven_r\nbackend: chat\noutput: json\n---\nX\n"})
    assert r.status_code == 400 and "IDC_SKILLS_DIR" in r.json()["detail"]


def test_put_invalid_markdown_400(tmp_path):
    _overlay(tmp_path)
    # no frontmatter
    assert client.put("/api/skills/seven_r",
                      json={"markdown": "just text"}).status_code == 400
    # name in body doesn't match path
    r = client.put("/api/skills/seven_r", json={
        "markdown": "---\nname: other\nbackend: chat\noutput: json\n---\nX\n"})
    assert r.status_code == 400
    # empty
    assert client.put("/api/skills/seven_r", json={"markdown": ""}).status_code == 400


def test_put_writes_overlay_and_run_uses_it(tmp_path):
    _overlay(tmp_path)
    md = ("---\nname: seven_r\ndescription: x\nbackend: chat\noutput: json\n"
          "max_rounds: 1\n---\nMARKDOWN BODY\n")
    r = client.put("/api/skills/seven_r", json={"markdown": md})
    assert r.status_code == 200 and r.json()["saved"] is True
    # source now overlay + the body is what's served
    g = client.get("/api/skills/seven_r").json()
    assert g["source"] == "overlay" and g["system"] == "MARKDOWN BODY"
    # run_skill picks up the overlay body (no restart) — the system prompt sent
    # to chat is the overlay body, proving the edit took effect live
    from idc.llm.runner import run_skill

    class _FakeLLM:
        def chat(self, messages, stream=False, timeout=None):
            self.sent = messages[0]["content"]
            return '{"strategy":"rehost"}'
    f = _FakeLLM()
    obj = run_skill("seven_r", "ctx", output="json", llm=f)
    assert obj["strategy"] == "rehost"
    assert f.sent == "MARKDOWN BODY"


def test_delete_overlay_reverts_to_builtin(tmp_path):
    _overlay(tmp_path)
    client.put("/api/skills/seven_r", json={
        "markdown": "---\nname: seven_r\nbackend: chat\noutput: json\n---\nX\n"})
    assert client.get("/api/skills/seven_r").json()["source"] == "overlay"
    r = client.delete("/api/skills/seven_r")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get("/api/skills/seven_r").json()["source"] == "builtin"


def test_delete_with_no_overlay_file_returns_false():
    r = client.delete("/api/skills/seven_r")
    assert r.status_code == 200 and r.json()["removed"] is False


# ---------------------------------------------------------------------------
# auth — /api/skills is operator-only (browser session when web_password set)
# ---------------------------------------------------------------------------
@pytest.fixture
def _auth_on():
    m.STORE.set_config("web_password", "test-pw")
    try:
        yield
    finally:
        with m.STORE.tx() as cur:
            cur.execute(m.STORE._x(
                "DELETE FROM system_config WHERE k='web_password'"))
        client.cookies.clear()


def test_skills_api_requires_session(_auth_on):
    # auth on, no session -> 401 (operator-only; a leaked executor bearer can't
    # rewrite LLM behavior because /api/skills isn't in _BEARER_PREFIXES)
    assert client.get("/api/skills").status_code == 401
    # login opens it
    r = client.post("/login", data={"password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/skills").status_code == 200
    # mutating endpoint also gated (would be 401 without the session we just got)
    assert client.put("/api/skills/seven_r", json={
        "markdown": "---\nname: seven_r\nbackend: chat\noutput: json\n---\nX\n"}
                      ).status_code in (200, 400)   # 400 = no overlay dir, not 401


def test_skills_api_bearer_not_honored(_auth_on):
    """A bearer token (executor credential) must NOT unlock skill editing —
    /api/skills is deliberately absent from _BEARER_PREFIXES."""
    r = client.get("/api/skills", headers={"Authorization": "Bearer exec-tok"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# operator-visible surfacing — agent (codex) findings flow to the host drawer
# summary, so an operator can SEE what the codex pass found (not just have it
# silently feed the LLM prompts). Used_by hints tell the operator what each
# skill drives.
# ---------------------------------------------------------------------------
def test_server_code_summary_surfaces_agent_fields():
    """The per-host code summary (what the host drawer renders) must carry the
    agent_findings count + blockers + summary, so the operator sees the codex
    pass output inline with the rule findings."""
    from idc.core.models import Server, Utilization, CodeProfile, ScanFinding
    s = Server(hostname="h", role="app", os="centos", app_ids=["APP_X"],
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    prof = CodeProfile(app_id="APP_X", language="java", cloud_readiness=0.5,
                       migration_pattern="replatform",
                       agent_findings=[ScanFinding(category="agent_insight",
                          severity="blocker", file="x.py", line=9, message="m")],
                       agent_blockers=["PL/SQL hard blocker"],
                       agent_summary="agent read the repo")
    entries = m._server_code_summary(s, {"APP_X": prof})
    assert len(entries) == 1
    e = entries[0]
    assert e["agent_findings_count"] == 1
    assert e["agent_blockers"] == ["PL/SQL hard blocker"]
    assert "agent read the repo" in e["agent_summary"]


def test_server_code_summary_omits_agent_fields_when_pass_off():
    """When the codex pass is off (no agent_* fields), the summary carries empty
    defaults — the drawer renders nothing extra (no 🤖 marker), so apps without
    the pass look unchanged."""
    from idc.core.models import Server, Utilization, CodeProfile
    s = Server(hostname="h", role="app", os="centos", app_ids=["APP_X"],
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    prof = CodeProfile(app_id="APP_X", language="java", cloud_readiness=0.9,
                       migration_pattern="rehost")
    e = m._server_code_summary(s, {"APP_X": prof})[0]
    assert e["agent_findings_count"] == 0 and e["agent_blockers"] == []
    assert e["agent_summary"] == ""