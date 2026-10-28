"""v9: project logger, automatic glossary, demo stories without repetition, documents-changed check,
filter scope for the clarification email / questions."""
from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler

import pytest

from ria.logging_setup import ROOT_LOGGER, ctx, get_logger, mask, setup_logging, tail
from ria.models.analysis import DocumentUnderstanding, FindingRecord, GlossaryTerm
from ria.models.enums import DetectedBy, DocumentType, FindingType, InputMode, Severity
from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions, auto_glossary
from ria.providers.mock import MockProvider, demo_capability, short_title
from ria.services.resolution import findings_in_scope, stakeholder_findings


@pytest.fixture
def logdir(tmp_path):
    d = setup_logging(level="INFO", directory=tmp_path / "logs", force=True)
    yield d
    for h in logging.getLogger(ROOT_LOGGER).handlers:
        h.flush()


def _read(d, name="ria.log"):
    for h in logging.getLogger(ROOT_LOGGER).handlers:
        h.flush()
    p = d / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ------------------------------------------------------------------ logger
def test_log_files_created_with_daily_rotation(logdir):
    get_logger("test").info("hello", extra=ctx("S1", "R1"))
    handlers = logging.getLogger(ROOT_LOGGER).handlers
    handlers = [h for h in handlers if isinstance(h, TimedRotatingFileHandler)]   # pytest adds capture handlers
    assert len(handlers) == 2
    assert {h.when for h in handlers} == {"MIDNIGHT"} and {h.backupCount for h in handlers} == {14}
    text = _read(logdir)
    assert "| INFO    | ria.test | session=S1 run=R1 | hello" in text


def test_setup_is_idempotent(logdir):
    first = list(logging.getLogger(ROOT_LOGGER).handlers)
    setup_logging(directory=logdir)
    setup_logging(directory=logdir)
    assert logging.getLogger(ROOT_LOGGER).handlers == first


def test_level_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RIA_LOG_LEVEL", "WARNING")
    d = setup_logging(directory=tmp_path / "l2", force=True)
    get_logger("x").info("not written")
    get_logger("x").warning("written")
    assert "not written" not in _read(d) and "written" in _read(d)


@pytest.mark.parametrize("secret,leak", [
    ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
    ("Basic cWFAYWNtZS50ZXN0OnRvaw==", "cWFAYWNtZS50ZXN0OnRvaw"),
    ("key xai-SUPERSECRET123456", "SUPERSECRET"),
    ("groq gsk_abcDEF1234567890", "abcDEF1234567890"),
    ("JIRA_API_TOKEN=ATATT3xFfGF0abc", "ATATT3xFfGF0abc"),
    ("mail rahul.mehta@northwind.example now", "rahul.mehta@northwind.example"),
    ("call +91 98765 43210 today", "98765 43210"),
])
def test_secrets_and_pii_masked(logdir, secret, leak):
    get_logger("privacy").warning("value: %s", secret)
    assert leak not in _read(logdir) and leak not in _read(logdir, "errors.log")
    assert leak not in mask(secret)


def test_timestamps_dates_and_counts_are_not_masked(logdir):
    get_logger("t").info("Run on 2026-10-26 16:54:24: tokens 18509 in + 9373 out, 13 segments, DOC-001")
    line = _read(logdir).strip().splitlines()[-1]
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \| INFO", line), line
    assert "2026-10-26 16:54:24" in line and "18509" in line and "9373" in line and "[phone]" not in line


@pytest.mark.parametrize("phone", ["+91 98765 43210", "+44 20 7946 0958", "98765 43210", "9876543210"])
def test_phone_formats_masked(phone):
    assert phone not in mask(f"call {phone} now") and "[phone]" in mask(f"call {phone} now")


def test_errors_log_gets_traceback_and_only_warnings(logdir):
    log = get_logger("err")
    log.info("just info")
    try:
        raise ValueError("token=abc123secret broke it")
    except ValueError:
        log.exception("Something failed")
    errors = _read(logdir, "errors.log")
    assert "Something failed" in errors and "Traceback" in errors and "ValueError" in errors
    assert "just info" not in errors and "abc123secret" not in errors


def test_full_analysis_logs_ids_and_counts_never_document_text(logdir, service, repo, session_id, settings):
    service.load_samples(session_id)
    service.confirm_all(session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(session_id, RunOptions(InputMode.COMPARE))
    from ria.models.enums import ItemType
    from ria.services.review import ReviewService
    ReviewService(repo).apply(session_id=session_id, run_id=run.run_id, item_type=ItemType.REQUIREMENT,
                              item_id="REQ-001", action="approve", reviewer="Nikhil", note="Finance said 45 days")
    text = _read(logdir)
    assert f"run={run.run_id}" in text and "Analysis started" in text and "Analysis COMPLETED" in text
    assert "AI call statements/" in text and "Document DOC-001 ingested" in text
    assert "Review: requirement REQ-001 PENDING -> APPROVED" in text
    for leaked in ("expense clam", "theek hai", "rahul.mehta@northwind", "Employees must submit",
                   "Finance said 45 days", "Nikhil", "expense_portal_brd.docx"):
        assert leaked not in text, leaked


def test_tail_returns_last_lines(logdir):
    for i in range(5):
        get_logger("t").info("line %d", i)
    _read(logdir)
    lines = tail(3)
    assert len(lines) == 3 and "line 4" in lines[-1]


# ------------------------------------------------------------------ automatic glossary
def _u(*terms):
    return DocumentUnderstanding(glossary=[GlossaryTerm(term=t, meaning=m, defined_in_source=m is not None)
                                           for t, m in terms])


def _doc(doc_id, dtype):
    from ria.models.segments import DocumentStats, SourceDocument
    return SourceDocument(document_id=doc_id, session_id="s", file_name=f"{doc_id}.txt", extension=".txt",
                          document_type=dtype, sha256="x" * 64, size_bytes=1, stats=DocumentStats())


def test_auto_glossary_prefers_authoritative_definitions_and_keeps_user_terms(repo, session_id):
    repo.replace_glossary(session_id, [("Claim", "Defined by the BA")])
    docs = [_doc("D1", DocumentType.MEETING_TRANSCRIPT), _doc("D2", DocumentType.BRD)]
    added = auto_glossary(repo, session_id, {
        "D1": _u(("GL code", "the code from the meeting"), ("claim", "meeting meaning"), ("Cost centre", None)),
        "D2": _u(("GL code", "General Ledger code"))}, docs)
    g = dict(repo.get_glossary(session_id))
    assert g["GL code"] == "General Ledger code"          # BRD definition wins over the transcript
    assert g["Claim"] == "Defined by the BA"              # existing definition never overwritten
    assert "Cost centre" in g and g["Cost centre"] is None
    assert added == 2
    assert auto_glossary(repo, session_id, {"D2": _u(("GL code", "other"))}, docs) == 0   # idempotent


# ------------------------------------------------------------------ demo stories
@pytest.mark.parametrize("text,expected", [
    ("The system shall send an email when a claim is approved.", "send an email when a claim is approved"),
    ("Receipts must be uploaded as PDF or image files", "to ensure that receipts must be uploaded as PDF or image files"),
    ("The employee selects a GL code", "to ensure that the employee selects a GL code"),
])
def test_demo_capability_is_natural(text, expected):
    assert demo_capability(text, None) == expected


def test_short_title():
    assert short_title("receipts must be uploaded as PDF or image files") == "Receipts must be uploaded as…"
    assert short_title("approve claims") == "Approve claims"


def test_demo_stories_do_not_repeat_requirement_text(service, repo, session_id, settings):
    service.load_samples(session_id)
    service.confirm_all(session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(session_id, RunOptions(InputMode.COMPARE))
    reqs = {r.requirement_id: r for r in repo.get_requirements(run.run_id)}
    tests = repo.get_scenarios(run.run_id)
    for s in repo.get_stories(run.run_id):
        core = reqs[s.requirement_ids[0]].description.rstrip(".").lower()[-30:]
        blob = " ".join([*(f"{a.given} {a.when} {a.then}" for a in s.acceptance_criteria),
                         *(f"{t.title} {t.expected_result}" for t in tests if t.story_id == s.story_id)]).lower()
        assert core not in blob, s.story_id                                 # only the narrative has it
        assert core in s.story_text.lower()
        assert len(s.title.rstrip("…").split()) <= 5 and "enforce" not in s.story_text


# ------------------------------------------------------------------ documents changed
def test_documents_changed(service, repo, session_id, settings):
    from ria.ui.common import documents_changed
    service.load_samples(session_id)
    service.confirm_all(session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(session_id, RunOptions(InputMode.COMPARE))
    run = repo.get_run(run.run_id)
    assert documents_changed(repo, session_id, run) == [] and documents_changed(repo, session_id, None) == []
    service.change_document_type(session_id, "DOC-003", DocumentType.REQUIREMENT_DOC)
    repo.delete_document(session_id, "DOC-004")
    service.ingest(session_id, "answers.txt", b"Q: A: 45 days.", DocumentType.CLARIFICATIONS)
    changes = documents_changed(repo, session_id, run)
    assert "added answers.txt" in changes and "removed DOC-004" in changes
    assert "type changed for travel_expense_policy.pdf" in changes


# ------------------------------------------------------------------ filter scope
def _f(fid, rids, ftype=FindingType.GAP, rule=None):
    return FindingRecord(finding_id=fid, run_id="r", finding_type=ftype, severity=Severity.MEDIUM, title="t",
                         description="d", detected_by=DetectedBy.RULE, requirement_ids=rids, rule=rule)


def test_findings_in_scope_and_stakeholder_filter():
    fs = [_f("F1", ["R1"]), _f("F2", ["R2"]), _f("F3", []), _f("F4", ["R1"], FindingType.AMBIGUITY, "vague_terms")]
    assert [f.finding_id for f in findings_in_scope(fs, None)] == ["F1", "F2", "F3", "F4"]
    assert [f.finding_id for f in findings_in_scope(fs, {"R1"})] == ["F1", "F4"]
    assert [f.finding_id for f in stakeholder_findings(fs)] == ["F1", "F2", "F3"]


def test_document_id_never_reused_after_removing_newest(service, repo, session_id):
    service.load_samples(session_id)
    repo.delete_document(session_id, "DOC-004")
    r = service.ingest(session_id, "answers.txt", b"Q: A: 45 days.", DocumentType.CLARIFICATIONS)
    assert r.document.document_id == "DOC-005"
    repo.delete_session(session_id)
    sid2 = repo.create_session("fresh")
    assert service.ingest(sid2, "a.txt", b"text", DocumentType.OTHER).document.document_id == "DOC-001"
