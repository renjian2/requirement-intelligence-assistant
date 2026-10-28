from __future__ import annotations

import pytest

from ria.models.analysis import DocumentUnderstanding, StatementExtractionResult
from ria.models.enums import DocumentType, EvidenceStatus, StatementType
from ria.providers.base import ChatMessage, ProviderError, RequestMeta
from ria.providers.factory import create_provider, describe_providers
from ria.providers.mock import MockProvider, fixture_files
from ria.validation.evidence import verify_quote

MSG = [ChatMessage("user", "x")]


def test_all_fixtures_match_schemas():
    schemas = {"DocumentUnderstanding": DocumentUnderstanding,
               "StatementExtractionResult": StatementExtractionResult}  # Stage 3 demo output is rule-based
    files = fixture_files()
    assert files
    for f in files:
        schemas[f.parent.name].model_validate_json(f.read_text().replace("{{CHUNK_ID}}", "DOC-001-C01"))


def test_mock_fixture_quotes_are_grounded_in_real_sample_segments(service, repo, session_id):
    """Fixtures must pass the same evidence check real LLM output will face."""
    service.load_samples(session_id)
    provider = MockProvider()
    checked = 0
    for doc in repo.list_documents(session_id):
        seg_text = {s.segment_id: s.text for s in repo.get_segments(session_id, doc.document_id)}
        for chunk in service.chunks_for(session_id, doc.document_id):
            resp = provider.generate_structured(MSG, StatementExtractionResult, meta=RequestMeta(
                stage="statements", document_id=doc.document_id, file_name=doc.file_name, chunk_id=chunk.chunk_id,
                citable_segment_ids=tuple(chunk.segment_ids)))
            assert resp.is_demo and resp.parsed.chunk_id == chunk.chunk_id
            for st in resp.parsed.statements:
                assert set(st.segment_ids) <= set(chunk.segment_ids)
                check = verify_quote(st.evidence_quote, st.segment_ids, seg_text)
                assert check.verified, (st.local_key, check)
                checked += 1
    assert checked == 26  # every fixture statement lands in exactly one chunk


def test_mock_marks_proposal_and_correction():
    resp = MockProvider().generate_structured(MSG, StatementExtractionResult, meta=RequestMeta(
        document_id="DOC-001", file_name="expense_portal_meeting.vtt", chunk_id="DOC-001-C01"))
    by_key = {s.local_key: s for s in resp.parsed.statements}
    assert by_key["s6"].evidence_status == EvidenceStatus.PROPOSED
    assert by_key["s9"].statement_type == StatementType.CORRECTION and by_key["s9"].supersedes_local_key == "s2"
    assert by_key["s2"].segment_ids == ["DOC-001-S0002"]


def test_mock_returns_empty_result_for_unknown_documents():
    resp = MockProvider().generate_structured(MSG, StatementExtractionResult, meta=RequestMeta(
        document_id="DOC-009", file_name="client_doc.docx", chunk_id="DOC-009-C01"))
    assert resp.parsed.statements == [] and "No demo fixture" in resp.note


def test_factory_requires_configuration(settings):
    assert create_provider("mock", settings).health_check().ok
    for name in ("grok", "ollama", "openai_compatible"):
        with pytest.raises(ProviderError, match="not configured"):
            create_provider(name, settings)


def test_describe_providers_never_exposes_keys(settings):
    from dataclasses import replace
    s = replace(settings, xai_api_key="xai-SUPERSECRETKEY1234", xai_model="grok-x")
    grok = next(p for p in describe_providers(s) if p.name.value == "grok")
    assert "SUPERSECRET" not in str(grok.details) and grok.configured and grok.sends_data_externally


def test_verify_quote_rules():
    seg = {"D-S1": "Employees must submit a claim within 60 days of the expense date."}
    assert verify_quote("submit a claim within 60 days", ["D-S1"], seg).verified
    assert not verify_quote("submit a claim within 30 days of payment approval", ["D-S1"], seg).verified
    assert not verify_quote("anything", ["D-S9"], seg).verified
    assert not verify_quote("", ["D-S1"], seg).verified
