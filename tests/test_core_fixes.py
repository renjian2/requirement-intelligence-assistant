"""Business statuses, Resolution Center buckets, session rules and carried-over provider fixes."""
from __future__ import annotations

import io
import json
import sys

import httpx
import pytest

from ria.models.analysis import FindingRecord, IssueDraft, RequirementDraft, RequirementRecord, StoryGenerationResult
from ria.models.enums import (
    DetectedBy, DocumentType, EvidenceStatus, FindingType, InputMode, RequirementType, ReviewStatus, Severity,
)
from ria.persistence.repositories import SessionNameError
from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions, check_input_mode
from ria.providers.http_provider import parse_duration, rate_limit_wait
from ria.providers.mock import MockProvider
from ria.providers.schema import strict_json_schema
from ria.services.resolution import Bucket, ReqStatus, bucket_findings, finding_bucket, requirement_statuses


def _req(rid="REQ-001", **kw):
    base = dict(requirement_id=rid, run_id="r", group="G", title="t", description="d",
                requirement_type=RequirementType.FUNCTIONAL, evidence_status=EvidenceStatus.STATED,
                source_statement_ids=["S1"], segment_ids=[], document_ids=[], actors=["Employee"])
    return RequirementRecord(**{**base, **kw})


def _f(ftype=FindingType.CONTRADICTION, rids=("REQ-001",), **kw):
    base = dict(finding_id="FND-001", run_id="r", finding_type=ftype, severity=Severity.HIGH, title="t",
                description="d", detected_by=DetectedBy.RULE, requirement_ids=list(rids))
    return FindingRecord(**{**base, **kw})


# ------------------------------------------------------------------ business statuses
@pytest.mark.parametrize("req,findings,expected", [
    (_req(), [], ReqStatus.REVIEW),
    (_req(evidence_status=EvidenceStatus.CONFLICTING), [], ReqStatus.CONFLICT),
    (_req(), [_f()], ReqStatus.CONFLICT),
    (_req(), [_f(FindingType.GAP)], ReqStatus.CLARIFY),
    (_req(vague_terms=["fast"]), [], ReqStatus.CLARIFY),
    (_req(evidence_status=EvidenceStatus.PROPOSED), [], ReqStatus.CLARIFY),
    (_req(review_status=ReviewStatus.NEEDS_CLARIFICATION), [], ReqStatus.CLARIFY),
    (_req(unresolved_questions=["Demo mode: details are added by AI"]), [], ReqStatus.REVIEW),
    (_req(review_status=ReviewStatus.APPROVED), [_f()], ReqStatus.APPROVED),           # human decision wins
    (_req(review_status=ReviewStatus.EDITED), [], ReqStatus.APPROVED),
    (_req(review_status=ReviewStatus.REJECTED), [_f()], ReqStatus.EXCLUDED),
    (_req(review_status=ReviewStatus.OUT_OF_SCOPE), [], ReqStatus.EXCLUDED),
    # contradiction resolved -> back to review even though evidence says CONFLICTING
    (_req(evidence_status=EvidenceStatus.CONFLICTING), [_f(review_status=ReviewStatus.RESOLVED)], ReqStatus.REVIEW),
    (_req(), [_f(FindingType.GAP, review_status=ReviewStatus.REJECTED)], ReqStatus.REVIEW),
])
def test_requirement_status(req, findings, expected):
    assert requirement_statuses([req], findings)[req.requirement_id] == expected


@pytest.mark.parametrize("finding,bucket", [
    (_f(), Bucket.CRITICAL),
    (_f(FindingType.GAP, rule="open_question"), Bucket.QUESTIONS),
    (_f(FindingType.GAP, rule="missing_actor"), Bucket.QUESTIONS),
    (_f(FindingType.UNDEFINED_TERM), Bucket.QUESTIONS),
    (_f(FindingType.SCOPE_ISSUE), Bucket.QUESTIONS),
    (_f(FindingType.AMBIGUITY, rule="amount_without_currency"), Bucket.QUESTIONS),
    (_f(FindingType.AMBIGUITY, rule="vague_terms"), Bucket.QUALITY),
    (_f(FindingType.POSSIBLE_TRANSCRIPTION_ERROR), Bucket.QUALITY),
    (_f(FindingType.DUPLICATE, detected_by=DetectedBy.LLM), Bucket.QUALITY),
])
def test_finding_bucket(finding, bucket):
    assert finding_bucket(finding) == bucket


def test_demo_run_buckets(service, repo, session_id, settings):
    service.load_samples(session_id)
    service.confirm_all(session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(session_id, RunOptions(InputMode.COMPARE))
    b = bucket_findings(repo.get_findings(run.run_id))
    assert len(b[Bucket.CRITICAL]) == 2 and all(f.finding_type == FindingType.CONTRADICTION for f in b[Bucket.CRITICAL])
    assert {f.rule for f in b[Bucket.QUALITY]} <= {"vague_terms", "transcription"} and b[Bucket.QUALITY]
    assert sum(len(v) for v in b.values()) == len(repo.get_findings(run.run_id))


# ------------------------------------------------------------------ sessions
def test_session_name_rules(repo):
    a = repo.create_session("Member Portal")
    for bad in ("member portal", " Member  Portal "):
        with pytest.raises(SessionNameError, match="already exists"):
            repo.create_session(bad)
    with pytest.raises(SessionNameError, match="enter a session name"):
        repo.create_session("  ")
    with pytest.raises(SessionNameError, match="too long"):
        repo.create_session("x" * 101)
    b = repo.create_session("Other")
    with pytest.raises(SessionNameError):
        repo.rename_session(b, "MEMBER portal")
    repo.rename_session(a, "MEMBER PORTAL")
    assert repo.unique_session_name("Other") == "Other (2)"


def test_delete_session_removes_everything(service, repo, session_id, settings):
    service.load_samples(session_id)
    service.confirm_all(session_id)
    run = AnalysisOrchestrator(repo, settings, MockProvider()).run(session_id, RunOptions(InputMode.COMPARE))
    repo.delete_session(session_id)
    assert repo.get_run(run.run_id) is None and repo.get_requirements(run.run_id) == []
    assert repo.get_stories(run.run_id) == [] and repo.get_llm_calls(session_id) == []


# ------------------------------------------------------------------ carried-over fixes
@pytest.mark.parametrize("model", [RequirementDraft, IssueDraft])
def test_schema_keeps_title(model):
    s = strict_json_schema(model)
    assert "title" in s["properties"] and "title" in s["required"] and "title" not in s


def test_schema_keeps_nested_title():
    story = strict_json_schema(StoryGenerationResult)["properties"]["stories"]["items"]
    assert "title" in story["properties"] and "title" in story["properties"]["test_scenarios"]["items"]["properties"]


@pytest.mark.parametrize("text,sec", [("7.66s", 7.66), ("450ms", 0.45), ("1m2.5s", 62.5), ("3", 3.0), (None, None)])
def test_parse_duration(text, sec):
    assert parse_duration(text) == (pytest.approx(sec) if sec is not None else None)


def test_rate_limit_exact_wait_and_daily_quota():
    from ria.models.analysis import StatementExtractionResult
    from ria.providers.base import ChatMessage, ProviderError
    from ria.providers.openai_compatible import OpenAICompatibleProvider
    assert rate_limit_wait(httpx.Response(429, text="Please try again in 7.66s.", headers={"retry-after": "9"})) == \
        pytest.approx(7.66)
    ok = httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"chunk_id": "C", "statements": []})}}]})
    seq, sleeps = iter([httpx.Response(429, text="try again in 2.5s"), ok]), []
    p = OpenAICompatibleProvider(base_url="https://x/v1", model="m", api_key="k", sleep=sleeps.append,
                                 transport=httpx.MockTransport(lambda r: next(seq)))
    p.generate_structured([ChatMessage("user", "u")], StatementExtractionResult)
    assert sleeps == [pytest.approx(2.5)]
    p2 = OpenAICompatibleProvider(base_url="https://x/v1", model="m", api_key="k", sleep=sleeps.append,
                                  transport=httpx.MockTransport(lambda r: httpx.Response(429, text="try again in 2h3m")))
    with pytest.raises(ProviderError, match="daily quota"):
        p2.generate_structured([ChatMessage("user", "u")], StatementExtractionResult)


def test_health_check_tokens():
    from ria.providers.openai_compatible import OpenAICompatibleProvider
    seen = {}

    def h(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    assert OpenAICompatibleProvider(base_url="https://x/v1", model="m", api_key="k",
                                    transport=httpx.MockTransport(h)).health_check().ok and seen["max_tokens"] == 60


def test_input_mode_message_names_file(service, repo, session_id):
    service.load_samples(session_id)
    brd = next(d for d in repo.list_documents(session_id) if d.document_type == DocumentType.BRD)
    service.confirm_extraction(session_id, brd.document_id)
    msg = " ".join(check_input_mode(InputMode.COMPARE, repo.list_documents(session_id)))
    assert "'expense_portal_meeting.vtt' (DOC-001) is waiting for confirmation" in msg


def test_eval_console_cp1252(monkeypatch):
    from eval.run_eval import _utf8_console
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    _utf8_console()
    print("\u2705 ok")
    sys.stdout.flush()
    assert "\u2705 ok".encode() in raw.getvalue()
