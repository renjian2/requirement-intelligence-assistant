"""Outlook email drafter and Jira client (no network: httpx.MockTransport, OS launchers mocked)."""
from __future__ import annotations

import base64
import email
import json
from email import policy

import httpx
import pytest

from ria.models.analysis import AcceptanceCriterion, ConflictValue, FindingRecord, StoryRecord
from ria.models.analysis import TestScenarioRecord as ScenarioRecord
from ria.models.enums import (
    CriterionType, DetectedBy, FindingType, InputMode, Priority, ReviewStatus, ScenarioType, Severity,
)
from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions
from ria.providers.mock import MockProvider
from ria.services.email_draft import (
    SUBJECT_TEMPLATE, build_email_draft, draft_path, open_in_outlook, suggested_recipients, to_eml,
)
from ria.services.jira_client import JiraClient, JiraConfig, build_issue_payload, label


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def analysed(service, repo, session_id, settings):
    service.load_samples(session_id)
    service.confirm_all(session_id)
    return AnalysisOrchestrator(repo, settings, MockProvider()).run(session_id, RunOptions(InputMode.COMPARE))


def _draft_for(repo, session_id, run, **kw):
    return build_email_draft(
        session_name="Expense portal", findings=repo.get_findings(run.run_id),
        requirements=repo.get_requirements(run.run_id),
        statements={s.statement_id: s for s in repo.get_statements(run.run_id)},
        documents={d.document_id: d for d in repo.list_documents(session_id)}, **kw)


def _finding(**kw):
    base = dict(finding_id="FND-001", run_id="r", finding_type=FindingType.CONTRADICTION, severity=Severity.HIGH,
                title="Deadline", description="d", detected_by=DetectedBy.RULE)
    return FindingRecord(**{**base, **kw})


def _story(**kw):
    base = dict(story_id="US-001", run_id="r", group="Claim submission", requirement_ids=["REQ-001"],
                title="Submit claim", actor="Employee", capability="submit a claim", benefit="I get reimbursed",
                acceptance_criteria=[AcceptanceCriterion(ac_id="AC-001-01", criterion_type=CriterionType.HAPPY_PATH,
                                                         given="a claim 45 days old", when="I submit",
                                                         then="it is accepted")])
    return StoryRecord(**{**base, **kw})


SCEN = [ScenarioRecord(scenario_id="TS-001", run_id="r", story_id="US-001", requirement_ids=["REQ-001"],
                       title="Submit in time", scenario_type=ScenarioType.POSITIVE, priority=Priority.HIGH,
                       steps=["Create claim", "Submit"], expected_result="Accepted"),
        ScenarioRecord(scenario_id="TS-002", run_id="r", story_id="US-001", requirement_ids=["REQ-001"],
                       title="Day 46", scenario_type=ScenarioType.BOUNDARY, priority=Priority.MEDIUM,
                       steps=["Submit late"], expected_result="Rejected")]


def _visible(html: str) -> str:
    return html.replace("&#8203;", "")


# ------------------------------------------------------------------ email content
def test_email_contains_contradictions_side_by_side_with_sources(repo, session_id, analysed):
    d = _draft_for(repo, session_id, analysed)
    assert d.subject == SUBJECT_TEMPLATE.format(session="Expense portal")
    deadline = next(c for c in d.conflicts if "time limit" in c.title)
    assert {v for v, _ in deadline.values} == {"45 days", "60 days", "90 days"}
    assert any("expense_portal_brd.docx" in src for _, src in deadline.values)
    html = _visible(d.html_body)
    assert "45 days" in html and "expense_portal_brd.docx" in html and "<table" in html
    assert d.questions and "Rahul Mehta" in d.recipients


def test_email_excludes_internal_wording_notes(repo, session_id, analysed):
    d = _draft_for(repo, session_id, analysed)
    text = d.text_body.lower()
    assert "user-friendly" not in text and "probably means 'claim'" not in text   # BA-only quality bucket
    assert "currency" in text                                                    # real business question kept


def test_email_html_escaping():
    f = _finding(title="<script>alert(1)</script>", values=[ConflictValue(requirement_id="R1", value="<b>x</b>")])
    html = build_email_draft(session_name="<i>S</i>", findings=[f]).html_body
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "&lt;i&gt;S&lt;/i&gt;" in html


def test_email_empty_findings_fallback():
    d = build_email_draft(session_name="", findings=[])
    assert d.is_empty and "Untitled session" in d.subject
    assert "No action is required" in d.html_body and d.recipients == ["Business owner", "Product Owner"]


def test_email_accepts_raw_dictionary_inputs():
    raw = _finding(document_ids=["D1"], values=[ConflictValue(requirement_id="R1", value="45 days",
                                                              document_ids=["D1"])]).model_dump(mode="json")
    d = build_email_draft(session_name="S", findings=[raw], documents={"D1": {"file_name": "brd.docx"}})
    assert d.conflicts[0].values == [("45 days", "brd.docx")]
    d2 = build_email_draft(session_name="S", findings=[raw], documents={"D1": "policy.pdf"})
    assert d2.conflicts[0].values == [("45 days", "policy.pdf")]


def test_email_missing_fields_degrade_gracefully():
    bare = _finding(values=[], ask_whom=None, clarification_question=None, business_impact=None,
                    document_ids=["DOC-404"])
    d = build_email_draft(session_name="S", findings=[bare])
    assert d.conflicts[0].values[0][1] == "DOC-404" and d.conflicts[0].question == "Which value is correct?"


def test_resolved_and_rejected_findings_excluded():
    d = build_email_draft(session_name="S", findings=[_finding(review_status=ReviewStatus.RESOLVED),
                                                      _finding(finding_id="FND-002", review_status=ReviewStatus.REJECTED)])
    assert d.is_empty


def test_email_html_is_outlook_safe_and_sections_numbered():
    html = build_email_draft(session_name="S", findings=[_finding(clarification_question="Which?")]).html_body
    assert 'bgcolor="#1F4E79"' in html and '<font color="#FFFFFF">' in html
    assert "<div" not in html and "<th" not in html
    assert "Conflicting Requirements" in html


def test_suggested_recipients_split_and_dedupe():
    f = [_finding(ask_whom="Rahul Mehta, Owner of brd.docx"), _finding(ask_whom="Rahul Mehta; Finance Lead")]
    assert suggested_recipients(f, []) == ["Rahul Mehta", "Owner of brd.docx", "Finance Lead"]


# ------------------------------------------------------------------ RFC 822 / MIME
def _eml(**kw):
    return to_eml(build_email_draft(session_name="Expense – é portal", findings=[_finding()], **kw))


def test_eml_x_unsent_header_and_valid_recipients_only():
    msg = email.message_from_bytes(_eml(to_addresses=["lead@example.com", "not-an-address"]), policy=policy.default)
    assert msg["X-Unsent"] == "1" and msg["To"] == "lead@example.com"
    assert msg["Subject"] == "[ACTION REQUIRED] Critical Requirements Clarifications & Gaps – Expense – é portal"


def test_eml_base64_parts_and_strict_crlf():
    raw = _eml()
    assert b"\n" not in raw.replace(b"\r\n", b"")                        # no bare LF anywhere
    msg = email.message_from_bytes(raw, policy=policy.default)
    parts = [p for p in msg.walk() if not p.is_multipart()]
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert all(p["Content-Transfer-Encoding"] == "base64" for p in parts)
    assert b"quoted-printable" not in raw.lower() and b"=20" not in raw
    html = base64.b64decode("".join(parts[1].get_payload().split())).decode("utf-8")
    assert html.startswith("<!DOCTYPE html>") and "</html>" in html


def test_eml_without_recipients_has_no_to_header():
    assert email.message_from_bytes(to_eml(build_email_draft(session_name="S", findings=[])),
                                    policy=policy.default)["To"] is None


# ------------------------------------------------------------------ launcher
def test_draft_path_is_timestamped_and_unique(tmp_path):
    from datetime import datetime
    a = draft_path(tmp_path, datetime(2026, 10, 26, 9, 0, 0, 1))
    b = draft_path(tmp_path, datetime(2026, 10, 26, 9, 0, 0, 2))
    assert a.name.startswith("Requirements_Critical_Findings_20261026_090000") and a != b


@pytest.fixture
def win(monkeypatch):
    import ria.services.email_draft as mod
    monkeypatch.setattr(mod.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(mod.os, "startfile", lambda p: calls.append(("startfile", p)), raising=False)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda args: calls.append(("popen", args)))
    return mod, calls


def _d():
    return build_email_draft(session_name="S", findings=[_finding()])


def test_windows_opens_in_classic_outlook_first(win, tmp_path, monkeypatch):
    mod, calls = win
    used = []
    monkeypatch.setattr(mod, "_open_with_com", lambda d: used.append(d.subject))
    ok, msg, path = open_in_outlook(b"x", tmp_path, draft=_d())
    assert ok and used and calls == [] and path.exists() and "Draft opened in Outlook" in msg


def test_windows_outlook_exe_then_startfile(win, tmp_path, monkeypatch):
    mod, calls = win
    monkeypatch.setattr(mod, "_open_with_com", lambda d: (_ for _ in ()).throw(ImportError("no pywin32")))
    monkeypatch.setattr(mod, "_find_outlook_exe", lambda: r"C:\Office\OUTLOOK.EXE")
    ok, _, path = open_in_outlook(b"x", tmp_path, draft=_d())
    assert ok and calls == [("popen", [r"C:\Office\OUTLOOK.EXE", "/eml", str(path)])]
    calls.clear()
    monkeypatch.setattr(mod, "_find_outlook_exe", lambda: None)
    ok, msg, path = open_in_outlook(b"x", tmp_path, draft=_d())
    assert ok and calls == [("startfile", str(path))] and "Outlook (classic)" in msg


def test_windows_all_methods_fail(win, tmp_path, monkeypatch):
    mod, _ = win
    monkeypatch.setattr(mod, "_open_with_com", lambda d: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(mod, "_find_outlook_exe", lambda: None)
    monkeypatch.setattr(mod.os, "startfile", lambda p: (_ for _ in ()).throw(OSError("locked")), raising=False)
    ok, msg, path = open_in_outlook(b"x", tmp_path, draft=_d())
    assert not ok and "Download .eml" in msg and path.exists()


@pytest.mark.parametrize("platform,opener", [("darwin", "open"), ("linux", "xdg-open")])
def test_mac_and_linux_openers(tmp_path, monkeypatch, platform, opener):
    import ria.services.email_draft as mod
    calls = []
    monkeypatch.setattr(mod.sys, "platform", platform)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda args: calls.append(args))
    ok, _, path = open_in_outlook(b"x", tmp_path, draft=_d())
    assert ok and calls == [[opener, str(path)]]


# ------------------------------------------------------------------ Jira payload
CLOUD = JiraConfig(base_url="https://acme.atlassian.net", email="qa@acme.test", api_token="tok",
                   project_key="RIA", simulation=False)


def test_textile_description(repo, session_id, analysed):
    reqs = {r.requirement_id: r for r in repo.get_requirements(analysed.run_id)}
    rid = next(iter(reqs))
    p = build_issue_payload(_story(requirement_ids=[rid]), reqs, SCEN, CLOUD)["fields"]
    d = p["description"]
    assert p["project"] == {"key": "RIA"} and p["issuetype"] == {"name": "Story"} and p["summary"] == "Submit claim"
    assert "bq. As a Employee, I want to submit a claim, so that I get reimbursed." in d
    assert f"* *{rid}* – {reqs[rid].title}" in d
    assert "* *AC-001-01* (HAPPY_PATH):\n** *Given* a claim 45 days old\n** *When* I submit\n** *Then* it is accepted" in d
    assert "## Create claim\n## Submit\n** *Expected:* Accepted" in d
    assert "_(Happy path)_" in d and "_(Edge case / error handling)_" in d


@pytest.mark.parametrize("raw,expected", [
    ("Claim submission", "Claim_submission"), ("A/B & C!", "A_B___C"),
    ("Claim submission (part 2) and more text", "Claim_submission__part_2__and"), ("   ", "RIA")])
def test_label_sanitisation(raw, expected):
    assert label(raw) == expected and len(label(raw)) <= 30


def test_labels_ria_approved_and_needs_review():
    approved = build_issue_payload(_story(review_status=ReviewStatus.APPROVED), {}, [], CLOUD)["fields"]["labels"]
    pending = build_issue_payload(_story(), {}, [], CLOUD)["fields"]["labels"]
    assert {"RIA", "ria-approved"} <= set(approved) and "needs-review" in pending
    assert all(" " not in x for x in approved + pending)


def test_payload_handles_missing_parts():
    p = build_issue_payload(_story(acceptance_criteria=[], requirement_ids=["REQ-404"], title="x" * 400),
                            {}, [], JiraConfig())["fields"]
    assert len(p["summary"]) <= 250 and p["project"]["key"] == "EXP"
    assert "title not available" in p["description"] and "No acceptance criteria yet" in p["description"]


# ------------------------------------------------------------------ Jira client
def test_simulation_keys_links_progress_and_no_network():
    def fail(req):
        raise AssertionError("network used in simulation")
    seen = []
    client = JiraClient(JiraConfig(simulation=True), transport=httpx.MockTransport(fail))
    assert client.test_connection()[0]
    res = client.create_stories([_story(), _story(story_id="US-002")], {}, SCEN,
                                progress=lambda done, total, r: seen.append((done, total, r.key)))
    assert [r.key for r in res] == ["EXP-101", "EXP-102"] and all(r.simulated for r in res)
    assert res[0].url == "https://your-domain.atlassian.net/browse/EXP-101"
    assert seen == [(1, 2, "EXP-101"), (2, 2, "EXP-102")]


def test_live_create_basic_auth_endpoint_and_browse_url():
    seen = {}

    def handler(req):
        seen.update(path=req.url.path, auth=req.headers["authorization"], body=json.loads(req.content))
        return httpx.Response(201, json={"id": "1", "key": "RIA-42"})
    res = JiraClient(CLOUD, transport=httpx.MockTransport(handler)).create_story(_story(), {}, SCEN)
    assert res.ok and res.url == "https://acme.atlassian.net/browse/RIA-42" and seen["path"] == "/rest/api/2/issue"
    assert seen["auth"] == "Basic " + base64.b64encode(b"qa@acme.test:tok").decode()


def test_data_center_pat_uses_bearer():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(201, json={"key": "DC-1"})
    cfg = JiraConfig(base_url="https://jira.corp", api_token="pat", project_key="DC", simulation=False)
    assert JiraClient(cfg, transport=httpx.MockTransport(handler)).create_story(_story(), {}, []).key == "DC-1"
    assert seen["auth"] == "Bearer pat"


def test_connection_checks_myself_and_project():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return httpx.Response(200, json={"displayName": "QA Bot", "name": "Expense"})
    ok, msg = JiraClient(CLOUD, transport=httpx.MockTransport(handler)).test_connection()
    assert ok and paths == ["/rest/api/2/myself", "/rest/api/2/project/RIA"] and "QA Bot" in msg


@pytest.mark.parametrize("status,body,expect", [
    (401, {}, "authentication"), (404, {"errorMessages": ["No project could be found with key 'RIA'."]}, "No project")])
def test_connection_errors(status, body, expect):
    def handler(req):
        if req.url.path.endswith("/myself") and status == 404:
            return httpx.Response(200, json={"displayName": "QA Bot"})
        return httpx.Response(status, json=body)
    ok, msg = JiraClient(CLOUD, transport=httpx.MockTransport(handler)).test_connection()
    assert not ok and expect in msg


def test_batch_continues_after_400_and_network_error():
    responses = iter([httpx.Response(400, json={"errorMessages": [], "errors": {"labels": "bad label"}}),
                      httpx.Response(201, json={"key": "RIA-2"})])
    res = JiraClient(CLOUD, transport=httpx.MockTransport(lambda r: next(responses))).create_stories(
        [_story(), _story(story_id="US-002")], {}, [])
    assert not res[0].ok and "labels: bad label" in res[0].error and res[1].key == "RIA-2"

    def down(req):
        raise httpx.ConnectError("down")
    assert "Network error" in JiraClient(CLOUD, transport=httpx.MockTransport(down)).create_story(_story(), {}, []).error


def test_live_mode_without_settings_fails_clearly():
    client = JiraClient(JiraConfig(simulation=False))
    assert "JIRA_BASE_URL" in client.test_connection()[1]
    assert "Missing settings" in client.create_story(_story(), {}, []).error
