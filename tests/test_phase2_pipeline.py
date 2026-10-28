"""Phase 2 pipeline: validation rules, grouping, redaction, glossary, orchestration."""
from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from ria.models.analysis import ExtractedStatement, RequirementDraft, StatementRecord
from ria.models.enums import (
    AnalysisMode, DocumentType, EvidenceStatus, InputMode, PrioritySource, RequirementType, RunStatus,
    SegmentKind, StatementType, SuggestionStatus,
)
from ria.models.segments import Segment
from ria.pipeline.grouping import GENERAL, build_groups
from ria.pipeline.orchestrator import (
    AnalysisOrchestrator, RunOptions, check_input_mode, documents_for_mode, estimate_run, suggest_input_mode,
)
from ria.pipeline.validators import validate_requirement, validate_statements
from ria.providers.grok import GrokProvider
from ria.providers.mock import MockProvider
from ria.services.redaction import NoRedaction, Redactor
from ria.validation.rules import find_vague_terms, is_hedged


def seg(sid, text, speaker=None, doc="DOC-001"):
    return Segment(segment_id=sid, document_id=doc, ordinal=1, kind=SegmentKind.SPEAKER_TURN, text=text,
                   speaker=speaker)


def stmt(sid, text, status=EvidenceStatus.STATED, type_=StatementType.BUSINESS_RULE, doc="DOC-001",
         verified=True, dtype=DocumentType.MEETING_TRANSCRIPT, **kw):
    return StatementRecord(statement_id=sid, run_id="r", document_id=doc, document_type=dtype, chunk_id="c",
                           statement_type=type_, text=text, evidence_status=status, segment_ids=[f"{doc}-S0001"],
                           evidence_quote=text, quote_verified=verified, quote_score=100.0, **kw)


def draft(**kw):
    base = dict(local_key="r1", title="t", description="The system shall do X.",
                requirement_type=RequirementType.FUNCTIONAL, evidence_status=EvidenceStatus.STATED,
                source_statement_ids=["STM-0001"])
    return RequirementDraft(**{**base, **kw})


# ------------------------------------------------------------------ statement validation
SEGS = {"D-S1": seg("D-S1", "Employees must submit a claim within 30 days.", "Rahul Mehta"),
        "D-S2": seg("D-S2", "Maybe we could send an SMS.", "Anita Desai"),
        "D-S9": seg("D-S9", "Not in this chunk.")}
TEXT = {k: v.text for k, v in SEGS.items()}


def _validate(statements, corrections=()):
    return validate_statements(statements, run_id="r", chunk_id="C1", document_id="DOC-001",
                               document_type=DocumentType.MEETING_TRANSCRIPT, citable_ids=["D-S1", "D-S2"],
                               sent_text=TEXT, segments=SEGS, start_number=1, redactor=NoRedaction(),
                               threshold=85, corrections=list(corrections))


def es(**kw):
    base = dict(local_key="s1", statement_type=StatementType.BUSINESS_RULE, text="x",
                evidence_status=EvidenceStatus.STATED, segment_ids=["D-S1"],
                evidence_quote="submit a claim within 30 days")
    return ExtractedStatement(**{**base, **kw})


def test_valid_statement_gets_id_speaker_from_segment():
    [r] = _validate([es(speaker="Wrong Person")])
    assert r.statement_id == "STM-0001" and r.quote_verified and r.speaker == "Rahul Mehta"


def test_citation_outside_chunk_removed_and_unsupported():
    [r] = _validate([es(segment_ids=["D-S9"])])
    assert r.evidence_status == EvidenceStatus.UNSUPPORTED and r.is_ai_suggestion and r.segment_ids == []


def test_invented_quote_becomes_unsupported():
    [r] = _validate([es(evidence_quote="submit a claim within 60 days of approval by finance")])
    assert r.evidence_status == EvidenceStatus.UNSUPPORTED and not r.quote_verified
    assert "Model said STATED" in r.validation_notes[0]


def test_hedged_quote_cannot_be_stated():
    [r] = _validate([es(segment_ids=["D-S2"], evidence_quote="Maybe we could send an SMS.")])
    assert r.evidence_status == EvidenceStatus.PROPOSED


def test_correction_links_and_glossary_flag():
    a = es(local_key="s1", evidence_quote="submit a claim within 30 days")
    b = es(local_key="s2", statement_type=StatementType.CORRECTION, supersedes_local_key="s1",
           evidence_quote="Employees must submit a claim")
    r1, r2 = _validate([a, b], corrections=[("submit", "submission")])
    assert r2.supersedes_id == "STM-0001" and r1.superseded_by_id == "STM-0002"
    assert r1.possible_transcription_issue and "accepted by reviewer" in r1.transcription_note


# ------------------------------------------------------------------ requirement validation
def _req(d, stmts, actors=()):
    return validate_requirement(d, run_id="r", group="G", group_statements={s.statement_id: s for s in stmts},
                                segments={"DOC-001-S0001": seg("DOC-001-S0001", "BR-01 | Priority: High")},
                                known_actors=list(actors), redactor=NoRedaction())


def test_proposed_sources_cannot_make_stated_requirement():
    r = _req(draft(), [stmt("STM-0001", "Send SMS", status=EvidenceStatus.PROPOSED)])
    assert r.evidence_status == EvidenceStatus.PROPOSED and r.confidence <= 60


def test_unknown_statement_ids_make_unsupported():
    r = _req(draft(source_statement_ids=["STM-9999"]), [stmt("STM-0001", "x")])
    assert r.evidence_status == EvidenceStatus.UNSUPPORTED and r.is_ai_suggestion and r.confidence <= 25


def test_actor_not_in_sources_moved_to_assumptions():
    r = _req(draft(actors=["Employee", "Auditor"]), [stmt("STM-0001", "Employee submits claim")])
    assert r.actors == ["Employee"] and any("Auditor" in a for a in r.assumptions)


def test_priority_stated_only_if_source_says_so():
    r = _req(draft(priority="HIGH", priority_stated=True), [stmt("STM-0001", "x")])
    assert r.priority_source == PrioritySource.STATED
    r2 = _req(draft(priority="LOW", priority_stated=True), [stmt("STM-0001", "x")])
    assert r2.priority_source == PrioritySource.AI_SUGGESTED


def test_vague_terms_and_confidence_band():
    r = _req(draft(description="Approvals should happen quickly"),
             [stmt("STM-0001", "approvals should happen quickly", status=EvidenceStatus.AMBIGUOUS)])
    assert r.vague_terms == ["quickly"] and r.confidence_band != "High"
    good = _req(draft(actors=["Employee"], trigger="On submit"),
                [stmt("STM-0001", "Employee submits", dtype=DocumentType.BRD),
                 stmt("STM-0002", "Employee submits", doc="DOC-002", dtype=DocumentType.BUSINESS_POLICY)])
    assert good.confidence_band == "High"


def test_rules():
    assert is_hedged("Maybe we could add SMS") and not is_hedged("Email is required")
    assert set(find_vague_terms("should load fast and be user-friendly")) == {"user-friendly", "fast"}


# ------------------------------------------------------------------ grouping
def test_grouping_keeps_correction_with_corrected_statement():
    a = stmt("STM-0001", "Submit within 30 days", superseded_by_id="STM-0003")
    b = stmt("STM-0002", "Manager approval needed")
    c = stmt("STM-0003", "Correction: submission window 45 days", type_=StatementType.CORRECTION,
             supersedes_id="STM-0001")
    groups = build_groups([a, b, c], {s.statement_id: s for s in (a, b, c)},
                          ["Approval workflow", "Claim submission"], max_size=30)
    sub = groups["Claim submission"]
    assert {s.statement_id for s in sub} == {"STM-0001", "STM-0003"}
    assert [s.statement_id for s in groups["Approval workflow"]] == ["STM-0002"]


def test_large_groups_are_split():
    many = [stmt(f"STM-{i:04d}", f"Rule {i}") for i in range(1, 8)]
    groups = build_groups(many, {s.statement_id: s for s in many}, [], max_size=3)
    assert list(groups) == [f"{GENERAL} (part 1)", f"{GENERAL} (part 2)", f"{GENERAL} (part 3)"]


# ------------------------------------------------------------------ redaction
def test_redaction_round_trip():
    r = Redactor(redact_names=True)
    r.register_speakers(["Rahul Mehta"])
    text = "Rahul said mail rahul.mehta@x.example or call +91 98765 43210. Rahul Mehta agreed."
    red = r.redact(text)
    assert "rahul.mehta@" not in red and "98765" not in red and "Rahul" not in red
    assert red.count("[PERSON_1]") == 2
    assert r.restore(red) == text.replace("Rahul said", "Rahul Mehta said")  # first name restores to full name


def _confirmed(service, repo, session_id):
    service.load_samples(session_id)
    for d in repo.list_documents(session_id):
        service.confirm_extraction(session_id, d.document_id)
    return repo.list_documents(session_id)


def test_input_modes(service, repo, session_id, settings):
    docs = _confirmed(service, repo, session_id)
    assert suggest_input_mode(docs) == InputMode.COMPARE
    assert all(d.document_type != DocumentType.BRD for d in documents_for_mode(InputMode.TRANSCRIPT_ONLY, docs))
    assert all(d.document_type != DocumentType.MEETING_TRANSCRIPT for d in documents_for_mode(InputMode.BRD_ONLY, docs))
    only_brd = [d for d in docs if d.document_type == DocumentType.BRD]
    assert check_input_mode(InputMode.COMPARE, only_brd)
    assert not check_input_mode(InputMode.BRD_ONLY, only_brd)
    from ria.pipeline.chunker import build_chunks
    chunks = {d.document_id: build_chunks(d.document_id, repo.get_segments(session_id, d.document_id), 300,
                                          d.detected_transcript) for d in docs}
    est = estimate_run(docs, chunks, AnalysisMode.DEEP, settings)
    assert est.calls == sum(est.per_stage.values()) and est.input_tokens > 0


# ------------------------------------------------------------------ orchestration
def test_mock_run_end_to_end(service, repo, session_id, settings):
    _confirmed(service, repo, session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(
        session_id, RunOptions(InputMode.COMPARE, AnalysisMode.DEEP, redact=True))
    assert run.status == RunStatus.COMPLETED and run.is_demo
    stmts = repo.get_statements(run.run_id)
    reqs = repo.get_requirements(run.run_id)
    assert len(stmts) == 26 and all(s.quote_verified for s in stmts)
    assert [r.requirement_id for r in reqs] == [f"REQ-{i:03d}" for i in range(1, len(reqs) + 1)]
    sms = next(r for r in reqs if "SMS" in r.description)
    assert sms.evidence_status == EvidenceStatus.PROPOSED            # planted issue P08
    window = next(r for r in reqs if "45 days" in r.description)
    old = next(s for s in stmts if "30 days" in s.text)
    assert old.statement_id in window.source_statement_ids and old.superseded_by_id  # correction kept
    assert not any(r.description.startswith("Employees must submit an expense claim within 30") for r in reqs)
    assert any("quickly" in r.vague_terms for r in reqs)               # planted issue P04
    assert repo.get_understandings(run.run_id)


def test_quick_mode_skips_understanding(service, repo, session_id, settings):
    _confirmed(service, repo, session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(
        session_id, RunOptions(InputMode.TRANSCRIPT_ONLY, AnalysisMode.QUICK))
    assert run.status == RunStatus.COMPLETED and not repo.get_understandings(run.run_id)
    assert "DOC-002" not in run.summary.documents  # BRD excluded in mode A


class FakeLLM:
    """Fake OpenAI-compatible server that answers each stage from the prompt content."""

    def __init__(self, fail_401=False, too_long_over=None):
        self.calls, self.fail_401, self.too_long_over = 0, fail_401, too_long_over
        self.prompts = []

    def __call__(self, req):
        self.calls += 1
        if self.fail_401:
            return httpx.Response(401, text="bad key")
        body = json.loads(req.content)
        prompt = body["messages"][-1]["content"]
        self.prompts.append(prompt)
        name = body["response_format"]["json_schema"]["name"]
        if name == "DocumentUnderstanding":
            out = {"modules": ["Approval workflow"], "actors": ["Employee", "Line Manager"]}
        elif name == "StatementExtractionResult":
            segs = json.loads(prompt.rsplit("<source_segments>", 1)[1].split("</source_segments>")[0])
            if self.too_long_over and len(segs) > self.too_long_over:
                return httpx.Response(400, text="maximum context length exceeded")
            chunk_id = prompt.split('chunk_id = "')[1].split('"')[0]
            out = {"chunk_id": chunk_id, "statements": [
                {"local_key": f"s{i}", "statement_type": "BUSINESS_RULE", "text": s["text"][:80],
                 "evidence_status": "STATED", "segment_ids": [s["id"]],
                 "evidence_quote": " ".join(s["text"].split()[:12])} for i, s in enumerate(segs[:3])]}
        elif name == "IssueReviewResult":
            reqs = json.loads(prompt.rsplit("<requirements>", 1)[1].split("</requirements>")[0])
            out = {"group": "g", "findings": [
                {"local_key": "f1", "finding_type": "GAP", "severity": "HIGH", "title": "No rejection flow",
                 "description": "What happens on rejection is not described.", "requirement_ids": [reqs[0]["id"]],
                 "clarification_question": "What happens when the claim is rejected?", "ask_whom": "Line Manager"},
                {"local_key": "f2", "finding_type": "GAP", "severity": "LOW", "title": "Invented reference",
                 "description": "x", "requirement_ids": ["REQ-999"], "clarification_question": "?"},
                {"local_key": "f3", "finding_type": "CONTRADICTION", "severity": "HIGH", "title": "Needs two",
                 "description": "x", "requirement_ids": [reqs[0]["id"]], "clarification_question": "?"}]}
        elif name == "StoryGenerationResult":
            reqs = json.loads(prompt.rsplit("<requirements>", 1)[1].split("</requirements>")[0])
            out = {"group": "g", "stories": [{
                "local_key": "u1", "requirement_ids": [reqs[0]["id"], "REQ-999"], "title": "Story",
                "actor": "Auditor", "capability": "do the thing", "benefit": None,
                "acceptance_criteria": [{"criterion_type": "HAPPY_PATH", "given": "a claim", "when": "submitted",
                                         "then": "it is accepted within 7 days"}],
                "test_scenarios": [{"title": f"T{i}", "scenario_type": "POSITIVE", "priority": p,
                                    "steps": ["step"], "expected_result": "ok"}
                                   for i, p in enumerate(["LOW", "HIGH", "MEDIUM", "HIGH", "LOW"])]}]}
        else:
            stm = json.loads(prompt.rsplit("<statements>", 1)[1].split("</statements>")[0])
            out = {"group": "g", "requirements": [{
                "local_key": "r1", "title": "Approval rule", "description": stm[0]["text"],
                "requirement_type": "BUSINESS", "evidence_status": "STATED", "actors": ["Line Manager", "Auditor"],
                "source_statement_ids": [stm[0]["id"], "STM-9999"]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


def _grok(fake):
    return GrokProvider(base_url="https://api.x.ai/v1", model="grok-test", api_key="k",
                        transport=httpx.MockTransport(fake), sleep=lambda s: None)


def test_real_provider_path_with_cache_and_redaction(service, repo, session_id, settings):
    _confirmed(service, repo, session_id)
    fake = FakeLLM()
    run = AnalysisOrchestrator(repo, settings, _grok(fake)).run(
        session_id, RunOptions(InputMode.COMPARE, AnalysisMode.DEEP, redact=True))
    assert run.status == RunStatus.COMPLETED and not run.is_demo
    assert run.summary.input_tokens == 100 * fake.calls and not run.summary.tokens_estimated
    assert not any("rahul.mehta@northwind" in p or "Priya Shah" in p for p in fake.prompts)
    stmts = repo.get_statements(run.run_id)
    assert stmts and all(s.quote_verified for s in stmts)
    assert any("Priya Shah" == s.speaker for s in stmts)  # restored locally from segment metadata
    req = repo.get_requirements(run.run_id)[0]
    assert "STM-9999" not in req.source_statement_ids and any("Auditor" in a for a in req.assumptions)
    first_calls = fake.calls
    run2 = AnalysisOrchestrator(repo, settings, _grok(fake)).run(
        session_id, RunOptions(InputMode.COMPARE, AnalysisMode.DEEP, redact=True))
    assert fake.calls == first_calls and run2.summary.cached_calls == run2.summary.calls


def test_fatal_error_stops_run(service, repo, session_id, settings):
    _confirmed(service, repo, session_id)
    fake = FakeLLM(fail_401=True)
    run = AnalysisOrchestrator(repo, settings, _grok(fake)).run(session_id, RunOptions(InputMode.COMPARE))
    assert run.status == RunStatus.FAILED and fake.calls == 1 and "authentication" in run.summary.errors[0]
    assert repo.get_llm_calls(session_id)[0]["status"] == "ERROR"


def test_context_too_long_splits_chunk(service, repo, session_id, settings):
    _confirmed(service, repo, session_id)
    fake = FakeLLM(too_long_over=4)
    run = AnalysisOrchestrator(repo, settings, _grok(fake)).run(
        session_id, RunOptions(InputMode.TRANSCRIPT_ONLY, AnalysisMode.QUICK))
    assert run.status == RunStatus.COMPLETED_WITH_ERRORS or run.status == RunStatus.COMPLETED
    chunk_ids = {s.chunk_id for s in repo.get_statements(run.run_id)}
    assert any(c.endswith(("a", "b")) for c in chunk_ids)


def test_phase1_database_is_migrated(tmp_path):
    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);"
                      "INSERT INTO sessions VALUES ('abc', 'old', '2026-10-20');")
    con.commit(); con.close()
    from ria.persistence.repositories import Repository
    repo = Repository(db)
    repo.set_input_mode("abc", InputMode.COMPARE)
    assert repo.get_input_mode("abc") == InputMode.COMPARE
    assert repo.create_session("new")
