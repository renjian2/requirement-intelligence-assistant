"""Streamlit AppTest: Home + 4 milestone pages render, BA workflow controls work, tables are PyArrow-safe."""
from __future__ import annotations

import re

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import ROOT

PAGES = ["Home.py", "pages/1_Prepare_and_Run.py", "pages/2_Review_and_Analysis.py",
         "pages/3_Stories_and_Tests.py", "pages/4_Export.py"]


def _kind(v):
    if isinstance(v, bool):
        return "bool"
    return "number" if isinstance(v, (int, float)) else type(v).__name__


def assert_arrow_safe(data):
    """Each column holds one kind of value (plus None) and, if PyArrow works here, really converts."""
    rows = data if isinstance(data, list) else ([dict(zip(data, vals)) for vals in zip(*data.values())]
                                                if isinstance(data, dict) else [])
    kinds: dict[str, set] = {}
    for row in rows:
        for k, v in row.items():
            if v is not None:
                kinds.setdefault(k, set()).add(_kind(v))
    mixed = {k: v for k, v in kinds.items() if len(v) > 1}
    assert not mixed, f"Mixed value types in table columns: {mixed}"
    try:
        import pyarrow as pa
    except ImportError:          # some environments ship a pyarrow that needs a newer NumPy
        return
    if rows:
        pa.Table.from_pylist(rows)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("RIA_DATA_DIR", str(tmp_path))
    for key in ("XAI_API_KEY", "XAI_MODEL", "OLLAMA_MODEL", "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODEL", "RIA_DEFAULT_PROVIDER", "RIA_ANALYSIS_DEPTH", "JIRA_SIMULATION", "JIRA_PROJECT_KEY", "JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    import streamlit as st

    def checked(data=None, *a, **k):
        assert_arrow_safe(data if data is not None else [])
        return st.json(data if data is not None else [])
    monkeypatch.setattr(st, "dataframe", checked)
    monkeypatch.setattr(st, "table", checked)
    from ria.ui import common
    common.get_services.clear()


def _open(page: str, sid: str | None = None, timeout: int = 90, state: dict | None = None) -> AppTest:
    at = AppTest.from_file(str(ROOT / "Home.py"), default_timeout=timeout)
    if sid:
        at.session_state["session_id"] = sid
    for k, v in (state or {}).items():
        at.session_state[k] = v
    at.run()
    if page != "Home.py":
        at.switch_page(page).run()
    return at


def _demo(analysed: bool = False, name: str = "Demo"):
    from ria.config import get_settings
    from ria.models.enums import InputMode
    from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions
    from ria.providers.mock import MockProvider
    from ria.ui.common import get_services
    repo, service = get_services()
    sid = repo.create_session(name)
    service.load_samples(sid)
    run = None
    if analysed:
        service.confirm_all(sid)
        run = AnalysisOrchestrator(repo, get_settings(), MockProvider()).run(sid, RunOptions(InputMode.COMPARE))
    return repo, service, sid, run


def _btn(at, label=None, key=None, startswith=None):
    return next(b for b in at.button if (label is None or b.label == label) and (key is None or b.key == key)
                and (startswith is None or b.label.startswith(startswith)))


def _texts(at) -> str:
    return "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption])


# ------------------------------------------------------------------ render
@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_session(page):
    assert not _open(page).exception


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_analysed_session(page):
    _, _, sid, _ = _demo(True)
    at = _open(page, sid)
    assert not at.exception, (page, at.exception)


def test_only_home_and_four_pages_and_links_resolve():
    assert sorted(p.name for p in (ROOT / "pages").glob("*.py")) == [
        "1_Prepare_and_Run.py", "2_Review_and_Analysis.py", "3_Stories_and_Tests.py", "4_Export.py"]
    targets = set()
    for path in [ROOT / "Home.py", *(ROOT / "pages").glob("*.py"), *(ROOT / "ria").rglob("*.py")]:
        targets |= set(re.findall(r'(?:page_link|switch_page)\(\s*"([^"]+\.py)"', path.read_text(encoding="utf-8")))
    assert targets
    for t in targets:
        assert (ROOT / t).exists(), f"broken link: {t}"


# ------------------------------------------------------------------ Home
def test_home_history_open_session_and_milestones():
    repo, _, sid, _ = _demo(True)
    at = _open("Home.py")
    assert [p.label for p in at.get("page_link")] == [
        "Open Prepare & Run", "Open Review & Analysis", "Open Stories & Tests", "Open Export"]
    _btn(at, "📂 Open Session").click().run()
    assert at.session_state["session_id"] == sid
    assert [m.label for m in at.metric][:4] == ["Documents", "Requirements", "Open issues", "User stories"]


def test_home_delete_with_confirmation_and_duplicate_names():
    from ria.ui.common import get_services
    repo, service = get_services()
    keep = repo.create_session("Keep me")
    gone = repo.create_session("Delete me")
    at = _open("Home.py", gone)
    _btn(at, key=f"delete_{gone}").click().run()
    _btn(at, "Cancel").click().run()
    assert repo.get_session(gone)
    _btn(at, key=f"delete_{gone}").click().run()
    _btn(at, "Yes, delete permanently").click().run()
    assert repo.get_session(gone) is None and repo.get_session(keep) and "session_id" not in at.session_state
    at = _open("Home.py")
    next(t for t in at.text_input if t.label == "Session name").set_value("  keep   ME ")
    _btn(at, "Create session").click().run()
    assert any("already exists" in e.value for e in at.error) and len(repo.list_sessions()) == 1


# ------------------------------------------------------------------ Page 1 · Prepare & Run
def test_prepare_has_no_mode_depth_or_glossary_widgets():
    _, _, sid, _ = _demo(False)
    at = _open("pages/1_Prepare_and_Run.py", sid)
    labels = [w.label for w in [*at.selectbox, *at.radio, *at.text_area, *at.slider]]
    assert "Input mode" not in labels and "Depth" not in labels
    assert not at.radio and not at.text_area                                  # no depth radio, no glossary editor
    assert not any("Glossary" in t.label or "segments" in t.label.lower() for t in at.tabs)
    assert [e.label for e in at.expander] == ["⚙️ Advanced settings"]


def test_one_click_run_auto_confirms_deep_and_builds_glossary():
    repo, _, sid, _ = _demo(False)
    at = _open("pages/1_Prepare_and_Run.py", sid)
    _btn(at, "⚡ Run Complete AI Analysis").click().run(timeout=120)
    assert not at.exception, at.exception
    assert all(d.status.value == "CONFIRMED" for d in repo.list_documents(sid))
    run = repo.latest_run(sid)
    assert run.status.value == "COMPLETED" and run.analysis_mode == "DEEP" and run.input_mode.value == "COMPARE"
    assert run.summary.stages == ["understanding", "statements", "requirements", "issues", "stories"]
    assert ("GL code", None) in repo.get_glossary(sid)                       # automatic glossary
    assert any("Analysis complete" in s.value for s in at.success)


def test_type_dropdown_changes_document_type():
    from ria.models.enums import DocumentType
    repo, _, sid, _ = _demo(False)
    at = _open("pages/1_Prepare_and_Run.py", sid)
    at.selectbox(key="type_DOC-002").set_value(DocumentType.MEETING_NOTES).run()
    assert repo.get_document(sid, "DOC-002").document_type == DocumentType.MEETING_NOTES
    assert not at.exception


def test_documents_changed_warning_after_run():
    from ria.models.enums import DocumentType
    repo, service, sid, _ = _demo(True)
    assert not any("Documents changed" in w.value for w in _open("pages/1_Prepare_and_Run.py", sid).warning)
    service.ingest(sid, "clarification_answers.txt", b"Q: Window? A: 45 days confirmed.", DocumentType.CLARIFICATIONS)
    at = _open("pages/1_Prepare_and_Run.py", sid)
    assert any("Documents changed since the last analysis" in w.value and "clarification_answers.txt" in w.value
               for w in at.warning)


def test_demo_provider_hidden_when_real_provider_configured(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    _, _, sid, _ = _demo(False)
    at = _open("pages/1_Prepare_and_Run.py", sid)
    provider = at.selectbox(key="adv_provider")
    assert [o for o in provider.options] == ["Ollama – local model"]


def test_remove_document_button():
    repo, _, sid, _ = _demo(False)
    at = _open("pages/1_Prepare_and_Run.py", sid)
    _btn(at, key="rm_DOC-004").click().run()
    assert [d.document_id for d in repo.list_documents(sid)] == ["DOC-001", "DOC-002", "DOC-003"]


# ------------------------------------------------------------------ Page 2 · Review & Analysis
def test_review_is_one_screen_without_resolution_center():
    _, _, sid, _ = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)
    assert not at.tabs or not any("Resolution" in t.label or "Critical" in t.label for t in at.tabs)
    assert "Stakeholder Resolution Center" not in _texts(at)
    assert {m.label: m.value for m in at.metric}["🔴 Conflict Detected"] == "5"
    assert not any(s.label == "Analysis run" for s in at.selectbox)           # single run -> no picker


def test_resolve_and_not_an_issue_in_deep_dive():
    repo, _, sid, run = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)                        # REQ-001 open by default
    at.text_input(key="note_FND-001").set_value("Finance confirmed 45 days")
    _btn(at, key="res_FND-001").click().run()
    f = next(x for x in repo.get_findings(run.run_id) if x.finding_id == "FND-001")
    assert f.review_status.value == "RESOLVED" and f.resolution == "Finance confirmed 45 days"
    _btn(at, key="dis_FND-014").click().run()
    assert next(x for x in repo.get_findings(run.run_id) if x.finding_id == "FND-014").review_status.value == "REJECTED"
    assert not at.exception


def test_resolve_requires_answer():
    repo, _, sid, run = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)
    _btn(at, key="res_FND-001").click().run()
    assert any("Enter the answer" in w.value for w in at.warning)
    assert next(x for x in repo.get_findings(run.run_id) if x.finding_id == "FND-001").review_status.value == "PENDING"


def test_email_button_respects_filter_and_excludes_wording_notes(monkeypatch):
    import ria.services.email_draft as mod
    opened = []
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_open_with_com", lambda draft: opened.append(draft))
    _, _, sid, _ = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)
    _btn(at, startswith="📧 Draft clarification email").click().run()
    _btn(at, "🚀 Open Directly in Outlook").click().run()
    full = opened[-1]
    assert len(full.conflicts) == 2 and "user-friendly" not in full.text_body.lower()
    at.multiselect[0].set_value(["🟢 Approved (Ready for Stories)"]).run()    # filter to approved only
    assert any("currently shown by your filters" in c.value for c in at.caption)
    _btn(at, "🚀 Open Directly in Outlook").click().run()
    narrow = opened[-1]
    assert not narrow.conflicts
    assert not at.exception


def test_questions_csv_respects_filter():
    _, _, sid, _ = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)
    full = next(d for d in at.get("download_button") if d.label.startswith("Download questions"))
    n_full = int(full.label.rsplit("·", 1)[1])
    at.multiselect[0].set_value(["🔴 Conflict Detected"]).run()
    narrow = next(d for d in at.get("download_button") if d.label.startswith("Download questions"))
    assert 0 < int(narrow.label.rsplit("·", 1)[1]) < n_full


def test_approve_area_and_bulk_and_request_changes():
    repo, _, sid, run = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)
    next(s for s in at.selectbox if s.label == "Requirement").set_value("REQ-012").run()
    _btn(at, startswith="✅ Approve all").click().run()
    approved = {r.requirement_id for r in repo.get_requirements(run.run_id) if r.review_status.value == "APPROVED"}
    assert "REQ-012" in approved and approved <= {r.requirement_id for r in repo.get_requirements(run.run_id)
                                                 if r.group == "Approval workflow"}
    next(t for t in at.text_input if t.label == "Note / requested change").set_value("Confirm the deadline")
    _btn(at, "Request changes").click().run()
    assert next(r for r in repo.get_requirements(run.run_id) if r.requirement_id == "REQ-012").review_status.value \
        == "NEEDS_CLARIFICATION"
    _btn(at, startswith="⚡ Approve").click().run()
    assert sum(r.review_status.value == "APPROVED" for r in repo.get_requirements(run.run_id)) >= 5


def test_reviewer_name_persists_across_pages_and_restarts():
    repo, _, sid, run = _demo(True)
    at = _open("pages/2_Review_and_Analysis.py", sid)
    next(t for t in at.text_input if t.label == "Reviewer name").set_value("Nikhil").run()
    _btn(at, key="appr_REQ-001").click().run()
    assert repo.get_audit(run.run_id)[-1]["reviewer"] == "Nikhil"
    fresh = _open("pages/3_Stories_and_Tests.py", sid)                      # new browser session
    assert next(t for t in fresh.text_input if t.label == "Reviewer name").value == "Nikhil"


# ------------------------------------------------------------------ Page 3 · Stories & Tests
def test_story_cards_do_not_repeat_requirement_text():
    repo, _, sid, run = _demo(True)
    at = _open("pages/3_Stories_and_Tests.py", sid, state={"story_scope": "All Stories"})
    req = next(r for r in repo.get_requirements(run.run_id) if r.requirement_id == "REQ-009")
    phrase = "receives an email when a claim is approved"
    assert phrase in req.description
    page = _texts(at) + "\n".join(s.label for s in at.expander)
    assert phrase in page
    assert "Functional Requirement:" in page
    assert "Path or Navigation:" in page
    assert "have the portal enforce" not in page and "applies the rule:" not in page
    assert any(c.value.startswith("Requirements: ") and "REQ-009" in c.value for c in at.caption)


def test_regenerate_shown_after_requirement_changes():
    from ria.models.enums import ItemType
    from ria.services.review import ReviewService
    repo, _, sid, run = _demo(True)
    at = _open("pages/3_Stories_and_Tests.py", sid)
    assert not any("Requirements changed after these stories" in w.value for w in at.warning)
    ReviewService(repo).apply(session_id=sid, run_id=run.run_id, item_type=ItemType.REQUIREMENT, item_id="REQ-009",
                              action="reject")
    at = _open("pages/3_Stories_and_Tests.py", sid)
    assert any("Requirements changed after these stories" in w.value for w in at.warning)
    _btn(at, "🔁 Regenerate stories & tests").click().run(timeout=120)
    assert not at.exception
    assert not any("REQ-009" in s.requirement_ids for s in repo.get_stories(run.run_id))
    assert any("regenerated" in s.value for s in at.success)
    assert not any("Requirements changed after these stories" in w.value for w in at.warning)


def test_story_board_scope_quick_edit_and_jira_push():
    from ria.models.enums import ItemType
    from ria.services.review import ReviewService
    repo, _, sid, run = _demo(True)
    story = repo.get_stories(run.run_id)[0]
    for rid in story.requirement_ids:
        ReviewService(repo).apply(session_id=sid, run_id=run.run_id, item_type=ItemType.REQUIREMENT, item_id=rid,
                                  action="approve")
    at = _open("pages/3_Stories_and_Tests.py", sid)
    assert any(f"{story.story_id} · " in m.value for m in at.markdown)       # sprint-ready
    _btn(at, key=f"s_appr_{story.story_id}").click().run()
    assert repo.get_stories(run.run_id)[0].review_status.value == "APPROVED"
    next(t for t in at.text_area if t.label == "Given").set_value("an edited precondition")
    _btn(at, "Save").click().run()
    assert repo.get_stories(run.run_id)[0].acceptance_criteria[0].given == "an edited precondition"
    _btn(at, key="stories_jira_test").click().run()
    _btn(at, startswith="🧪 Simulate creating").click().run()
    assert not at.exception and any("EXP-101" in r["message"] for r in repo.get_log(sid))


# ------------------------------------------------------------------ Page 4 · Export
def test_export_has_only_ba_deliverables_and_diagnostics():
    _, _, sid, _ = _demo(True)
    at = _open("pages/4_Export.py", sid)
    names = [d.label for d in at.get("download_button")]
    for expected in ("BRD (Word)", "Review report (Word)", "Excel workbook", "Jira (CSV)", "Download log"):
        assert any(expected in n for n in names), (expected, names)
    assert not any("JSON" in n for n in names)
    assert not at.tabs                                                       # no Jira / provider / log tabs
    assert not any(b.label.startswith(("🧪 Simulate", "🚀 Create in Jira", "🔍 Test Jira")) for b in at.button)
    assert [e.label for e in at.expander] == ["🛠️ Diagnostics"]
    assert any("Logging started" in c.value for c in at.code)                 # log tail shown
    at.radio[0].set_value("Final – approved items only").run()
    assert not at.exception


def test_run_picker_only_with_several_runs():
    from ria.config import get_settings
    from ria.models.enums import InputMode
    from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions
    from ria.providers.mock import MockProvider
    repo, _, sid, first = _demo(True)
    AnalysisOrchestrator(repo, get_settings(), MockProvider()).run(sid, RunOptions(InputMode.COMPARE))
    for page in PAGES[2:]:
        at = _open(page, sid)
        sb = next(s for s in at.selectbox if s.label == "Analysis run")
        assert len(set(sb.options)) == 2
        sb.set_value(first.run_id).run()
        assert not at.exception, (page, at.exception)
