from __future__ import annotations

import pytest

from ria.models.enums import DocumentType, ExtractionStatus, WarningCode
from ria.services.ingestion import (
    IngestionError, analysis_readiness, sanitise_file_name, suggest_document_type,
)


def test_ingest_persists_document_and_segments(service, repo, session_id):
    r = service.ingest(session_id, "notes.txt", b"First paragraph here.\n\nSecond paragraph.", DocumentType.MEETING_NOTES)
    assert r.document.document_id == "DOC-001"
    assert [s.segment_id for s in repo.get_segments(session_id, "DOC-001")] == ["DOC-001-S0001", "DOC-001-S0002"]
    assert repo.get_document(session_id, "DOC-001").status == ExtractionStatus.EXTRACTED


def test_duplicate_upload_is_skipped(service, session_id):
    service.ingest(session_id, "a.txt", b"Same content", DocumentType.OTHER)
    r = service.ingest(session_id, "b.txt", b"Same content", DocumentType.OTHER)
    assert r.document is None and r.duplicate_of.document_id == "DOC-001"


@pytest.mark.parametrize("name,data,msg", [
    ("virus.exe", b"x", "unsupported"),
    ("empty.txt", b"", "empty"),
])
def test_rejected_files(service, session_id, name, data, msg):
    with pytest.raises(IngestionError, match=msg):
        service.ingest(session_id, name, data, DocumentType.OTHER)


def test_size_limit(service, session_id, settings):
    with pytest.raises(IngestionError, match="larger"):
        service.ingest(session_id, "big.txt", b"x" * (settings.max_upload_bytes + 1), DocumentType.OTHER)


def test_failed_extraction_cannot_be_confirmed(service, session_id):
    r = service.ingest(session_id, "bad.docx", b"garbage", DocumentType.BRD)
    assert r.document.status == ExtractionStatus.FAILED
    with pytest.raises(IngestionError):
        service.confirm_extraction(session_id, r.document.document_id)


def test_change_type_recomputes_type_warnings(service, repo, session_id):
    service.load_samples(session_id)
    transcript = next(d for d in repo.list_documents(session_id) if d.file_name.endswith(".vtt"))
    updated = service.change_document_type(session_id, transcript.document_id, DocumentType.BRD)
    assert any(w.code == WarningCode.TYPE_MISMATCH for w in updated.warnings)
    back = service.change_document_type(session_id, transcript.document_id, DocumentType.MEETING_TRANSCRIPT)
    assert not any(w.code == WarningCode.TYPE_MISMATCH for w in back.warnings)


def test_readiness_gate_requires_confirmation(service, repo, session_id):
    ready, issues = analysis_readiness(repo, session_id)
    assert not ready
    r = service.ingest(session_id, "n.txt", b"Some requirement text.", DocumentType.MEETING_NOTES)
    assert not analysis_readiness(repo, session_id)[0]
    service.confirm_extraction(session_id, r.document.document_id)
    assert analysis_readiness(repo, session_id) == (True, [])


def test_delete_session_removes_everything(service, repo, session_id):
    service.load_samples(session_id)
    repo.delete_session(session_id)
    assert repo.get_session(session_id) is None
    assert repo.list_documents(session_id) == [] and repo.get_segments(session_id) == []
    assert repo.get_log(session_id) == []


def test_document_ids_not_reused_after_delete(service, repo, session_id):
    service.ingest(session_id, "a.txt", b"A text", DocumentType.OTHER)
    service.ingest(session_id, "b.txt", b"B text", DocumentType.OTHER)
    repo.delete_document(session_id, "DOC-001")
    r = service.ingest(session_id, "c.txt", b"C text", DocumentType.OTHER)
    assert r.document.document_id == "DOC-003"


def test_helpers():
    assert sanitise_file_name("../../etc/pa ss<wd>.txt") == "pa ss_wd_.txt"
    assert suggest_document_type("Teams_recording.vtt") == DocumentType.MEETING_TRANSCRIPT
    assert suggest_document_type("Claims_BRD_v2.docx") == DocumentType.BRD


def test_samples_load_cleanly(service, session_id):
    results = service.load_samples(session_id)
    assert len(results) == 4
    assert all(r.document.status == ExtractionStatus.EXTRACTED for r in results)
