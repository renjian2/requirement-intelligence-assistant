"""Repository functions for sessions, documents, segments and the run log."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ria.models.analysis import (
    AnalysisRun, DocumentUnderstanding, FindingRecord, RequirementRecord, StatementRecord, StoryRecord,
    TestScenarioRecord,
)
from ria.models.enums import DocumentType, ExtractionStatus, InputMode, SuggestionStatus
from ria.models.segments import DocumentStats, ExtractionWarning, Segment, SourceDocument
from ria.persistence.db import connect, init_db
from ria.logging_setup import ctx, get_logger

log = get_logger(__name__)


class SessionNameError(ValueError):
    """Empty, too long or duplicate session name (message is shown to the user)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        init_db(db_path)

    # ---- sessions -------------------------------------------------------
    @staticmethod
    def _name_key(name: str) -> str:
        return " ".join((name or "").split()).casefold()

    def session_name_exists(self, name: str, exclude_session_id: str | None = None) -> bool:
        key = self._name_key(name)
        return any(self._name_key(s["name"]) == key and s["session_id"] != exclude_session_id
                   for s in self.list_sessions())

    def unique_session_name(self, base: str) -> str:
        base = " ".join(base.split()) or "Untitled"
        if not self.session_name_exists(base):
            return base
        n = 2
        while self.session_name_exists(f"{base} ({n})"):
            n += 1
        return f"{base} ({n})"

    def _validate_name(self, name: str, exclude_session_id: str | None = None) -> str:
        clean = " ".join((name or "").split())
        if not clean:
            raise SessionNameError("Please enter a session name.")
        if len(clean) > 100:
            raise SessionNameError("Session name is too long (maximum 100 characters).")
        if self.session_name_exists(clean, exclude_session_id):
            raise SessionNameError(f"A session named '{clean}' already exists. Please choose a different name.")
        return clean

    def create_session(self, name: str) -> str:
        clean = self._validate_name(name)
        session_id = uuid.uuid4().hex[:12]
        with connect(self.db_path) as c:
            c.execute("INSERT INTO sessions(session_id, name, created_at) VALUES (?, ?, ?)",
                      (session_id, clean, _now()))
        self.log(session_id, "INFO", "session", f"Session '{clean}' created")
        log.info("Session created", extra=ctx(session_id))
        return session_id

    def list_sessions(self) -> list[dict]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM documents d WHERE d.session_id = s.session_id) AS doc_count "
                "FROM sessions s ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def rename_session(self, session_id: str, name: str) -> None:
        """Rename a session without touching its documents, runs or audit history."""
        clean = self._validate_name(name, exclude_session_id=session_id)
        with connect(self.db_path) as c:
            c.execute("UPDATE sessions SET name = ? WHERE session_id = ?", (clean, session_id))
        self.log(session_id, "INFO", "session", f"Session renamed to '{clean}'")
        log.info("Session renamed", extra=ctx(session_id))

    def session_last_activity(self, session_id: str) -> str | None:
        with connect(self.db_path) as c:
            row = c.execute("SELECT MAX(ts) AS last_activity FROM run_log WHERE session_id = ?", (session_id,)).fetchone()
        return row["last_activity"] if row else None

    def get_session(self, session_id: str) -> dict | None:
        with connect(self.db_path) as c:
            row = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def delete_session(self, session_id: str) -> None:
        """Delete a session and ALL its data (documents, segments, logs)."""
        with connect(self.db_path) as c:
            run_ids = [r["run_id"] for r in c.execute(
                "SELECT run_id FROM analysis_runs WHERE session_id = ?", (session_id,))]
            for table in ("understandings", "statements", "requirements", "llm_calls", "findings", "stories",
                          "scenarios", "audit_log"):
                c.executemany(f"DELETE FROM {table} WHERE run_id = ?", [(r,) for r in run_ids])
            for table in ("analysis_runs", "glossary", "transcription_suggestions"):
                c.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            c.execute("DELETE FROM segments WHERE session_id = ?", (session_id,))
            c.execute("DELETE FROM documents WHERE session_id = ?", (session_id,))
            c.execute("DELETE FROM run_log WHERE session_id = ?", (session_id,))
            c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            c.execute("DELETE FROM meta WHERE key = ?", (f"doc_counter:{session_id}",))
        log.info("Session deleted with %d analysis run(s)", len(run_ids), extra=ctx(session_id))

    def set_input_mode(self, session_id: str, mode: InputMode | None) -> None:
        with connect(self.db_path) as c:
            c.execute("UPDATE sessions SET input_mode = ? WHERE session_id = ?",
                      (mode.value if mode else None, session_id))

    def get_input_mode(self, session_id: str) -> InputMode | None:
        s = self.get_session(session_id)
        return InputMode(s["input_mode"]) if s and s.get("input_mode") else None

    # ---- glossary -------------------------------------------------------
    def replace_glossary(self, session_id: str, entries: list[tuple[str, str | None]]) -> None:
        with connect(self.db_path) as c:
            c.execute("DELETE FROM glossary WHERE session_id = ?", (session_id,))
            c.executemany("INSERT OR REPLACE INTO glossary VALUES (?,?,?)",
                          [(session_id, t, m) for t, m in entries])

    def get_glossary(self, session_id: str) -> list[tuple[str, str | None]]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT term, meaning FROM glossary WHERE session_id = ? ORDER BY term COLLATE NOCASE",
                             (session_id,)).fetchall()
        return [(r["term"], r["meaning"]) for r in rows]

    def save_suggestions(self, session_id: str, suggestions: list) -> None:
        """Insert new suggestions; keep the user's earlier ACCEPTED/REJECTED decisions."""
        with connect(self.db_path) as c:
            existing = {(r["original"], r["suggestion"]): r["status"] for r in c.execute(
                "SELECT original, suggestion, status FROM transcription_suggestions WHERE session_id = ?",
                (session_id,))}
            c.execute("DELETE FROM transcription_suggestions WHERE session_id = ? AND status = ?",
                      (session_id, SuggestionStatus.PENDING.value))
            for s in suggestions:
                status = existing.get((s.original, s.suggestion), SuggestionStatus.PENDING.value)
                c.execute("INSERT OR REPLACE INTO transcription_suggestions VALUES (?,?,?,?,?,?,?)",
                          (session_id, s.original, s.suggestion, s.score, json.dumps(s.segment_ids),
                           s.example, status))

    def get_suggestions(self, session_id: str) -> list[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM transcription_suggestions WHERE session_id = ? "
                             "ORDER BY status, score DESC", (session_id,)).fetchall()
        return [{**dict(r), "segment_ids": json.loads(r["segment_ids"])} for r in rows]

    def set_suggestion_status(self, session_id: str, original: str, suggestion: str,
                              status: SuggestionStatus) -> None:
        with connect(self.db_path) as c:
            c.execute("UPDATE transcription_suggestions SET status = ? WHERE session_id = ? AND original = ? "
                      "AND suggestion = ?", (status.value, session_id, original, suggestion))

    def accepted_corrections(self, session_id: str) -> list[tuple[str, str]]:
        return [(s["original"], s["suggestion"]) for s in self.get_suggestions(session_id)
                if s["status"] == SuggestionStatus.ACCEPTED.value]

    # ---- analysis runs ----------------------------------------------------
    def save_run(self, run: AnalysisRun) -> None:
        with connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO analysis_runs VALUES (?,?,?,?,?)",
                      (run.run_id, run.session_id, run.created_at.isoformat(timespec="seconds"),
                       run.status.value, run.model_dump_json()))

    def list_runs(self, session_id: str) -> list[AnalysisRun]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT json FROM analysis_runs WHERE session_id = ? ORDER BY created_at DESC, rowid DESC",
                             (session_id,)).fetchall()
        return [AnalysisRun.model_validate_json(r["json"]) for r in rows]

    def list_runs_all(self) -> list[AnalysisRun]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT json FROM analysis_runs ORDER BY created_at DESC").fetchall()
        return [AnalysisRun.model_validate_json(r["json"]) for r in rows]

    def get_run(self, run_id: str) -> AnalysisRun | None:
        with connect(self.db_path) as c:
            row = c.execute("SELECT json FROM analysis_runs WHERE run_id = ?", (run_id,)).fetchone()
        return AnalysisRun.model_validate_json(row["json"]) if row else None

    def latest_run(self, session_id: str) -> AnalysisRun | None:
        runs = self.list_runs(session_id)
        return runs[0] if runs else None

    def save_understanding(self, run_id: str, document_id: str, u: DocumentUnderstanding) -> None:
        with connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO understandings VALUES (?,?,?)", (run_id, document_id, u.model_dump_json()))

    def get_understandings(self, run_id: str) -> dict[str, DocumentUnderstanding]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT document_id, json FROM understandings WHERE run_id = ?", (run_id,)).fetchall()
        return {r["document_id"]: DocumentUnderstanding.model_validate_json(r["json"]) for r in rows}

    def save_statements(self, statements: list[StatementRecord]) -> None:
        with connect(self.db_path) as c:
            c.executemany("INSERT OR REPLACE INTO statements VALUES (?,?,?,?)",
                          [(s.run_id, s.statement_id, s.document_id, s.model_dump_json()) for s in statements])

    def get_statements(self, run_id: str) -> list[StatementRecord]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT json FROM statements WHERE run_id = ? ORDER BY statement_id", (run_id,)).fetchall()
        return [StatementRecord.model_validate_json(r["json"]) for r in rows]

    def save_requirements(self, reqs: list[RequirementRecord]) -> None:
        with connect(self.db_path) as c:
            c.executemany("INSERT OR REPLACE INTO requirements VALUES (?,?,?)",
                          [(r.run_id, r.requirement_id, r.model_dump_json()) for r in reqs])

    def get_requirements(self, run_id: str) -> list[RequirementRecord]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT json FROM requirements WHERE run_id = ? ORDER BY requirement_id",
                             (run_id,)).fetchall()
        return [RequirementRecord.model_validate_json(r["json"]) for r in rows]

    # ---- Phase 3 items (findings, stories, scenarios) ---------------------
    _ITEM_TABLES = {"findings": (FindingRecord, "finding_id"), "stories": (StoryRecord, "story_id"),
                    "scenarios": (TestScenarioRecord, "scenario_id")}

    def replace_items(self, table: str, run_id: str, items: list) -> None:
        model, key = self._ITEM_TABLES[table]
        with connect(self.db_path) as c:
            c.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            c.executemany(f"INSERT INTO {table} VALUES (?,?,?)",
                          [(run_id, getattr(i, key), i.model_dump_json()) for i in items])

    def save_item(self, table: str, item) -> None:
        if table == "requirements":
            self.save_requirements([item]); return
        _, key = self._ITEM_TABLES[table]
        with connect(self.db_path) as c:
            c.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)",
                      (item.run_id, getattr(item, key), item.model_dump_json()))

    def get_items(self, table: str, run_id: str) -> list:
        model, _ = self._ITEM_TABLES[table]
        with connect(self.db_path) as c:
            rows = c.execute(f"SELECT json FROM {table} WHERE run_id = ? ORDER BY item_id", (run_id,)).fetchall()
        return [model.model_validate_json(r["json"]) for r in rows]

    def get_findings(self, run_id: str) -> list[FindingRecord]:
        return self.get_items("findings", run_id)

    def get_stories(self, run_id: str) -> list[StoryRecord]:
        return self.get_items("stories", run_id)

    def get_scenarios(self, run_id: str) -> list[TestScenarioRecord]:
        return self.get_items("scenarios", run_id)

    def add_audit(self, *, run_id: str, session_id: str, item_type: str, item_id: str, action: str,
                  field: str | None = None, old: str | None = None, new: str | None = None,
                  reviewer: str | None = None, note: str | None = None) -> None:
        with connect(self.db_path) as c:
            c.execute("INSERT INTO audit_log(run_id, session_id, ts, item_type, item_id, action, field, old_value, "
                      "new_value, reviewer, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (run_id, session_id, _now(), item_type, item_id, action, field,
                       None if old is None else str(old)[:4000], None if new is None else str(new)[:4000],
                       reviewer, note))

    def last_audit_id(self, run_id: str) -> int:
        with connect(self.db_path) as c:
            row = c.execute("SELECT MAX(id) AS m FROM audit_log WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["m"] or 0)

    def get_audit(self, run_id: str) -> list[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def log_llm_call(self, *, run_id: str, session_id: str, stage: str, ref: str | None, provider: str,
                     model: str, prompt_version: str | None, input_tokens: int, output_tokens: int,
                     estimated: bool, latency_ms: int, cached: bool, status: str, message: str = "") -> None:
        with connect(self.db_path) as c:
            c.execute("INSERT INTO llm_calls(run_id, session_id, ts, stage, ref, provider, model, prompt_version, "
                      "input_tokens, output_tokens, estimated, latency_ms, cached, status, message) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (run_id, session_id, _now(), stage, ref, provider, model, prompt_version, input_tokens,
                       output_tokens, int(estimated), latency_ms, int(cached), status, message[:1000]))

    def get_llm_calls(self, session_id: str, run_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM llm_calls WHERE session_id = ?", [session_id]
        if run_id:
            sql += " AND run_id = ?"; params.append(run_id)
        with connect(self.db_path) as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT 1000", params).fetchall()
        return [dict(r) for r in rows]

    # ---- LLM response cache ---------------------------------------------
    def cache_get(self, key: str) -> dict | None:
        with connect(self.db_path) as c:
            row = c.execute("SELECT * FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def cache_put(self, key: str, provider: str, model: str, raw_text: str, input_tokens: int,
                  output_tokens: int) -> None:
        with connect(self.db_path) as c:
            c.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?,?,?)",
                      (key, provider, model, raw_text, input_tokens, output_tokens, _now()))

    def cache_clear(self) -> int:
        with connect(self.db_path) as c:
            n = c.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
            c.execute("DELETE FROM llm_cache")
        return n

    # ---- documents ------------------------------------------------------
    def next_document_id(self, session_id: str) -> str:
        """Never reuses an ID, even after the newest document was removed: old runs, evidence links and the
        audit trail still refer to it. The highest number ever issued is kept in the meta table."""
        with connect(self.db_path) as c:
            rows = c.execute("SELECT document_id FROM documents WHERE session_id = ?", (session_id,)).fetchall()
            row = c.execute("SELECT value FROM meta WHERE key = ?", (f"doc_counter:{session_id}",)).fetchone()
        used = [int(r["document_id"].split("-")[1]) for r in rows]
        issued = int(row["value"]) if row else 0
        return f"DOC-{(max([issued, *used]) + 1):03d}"

    def find_by_hash(self, session_id: str, sha256: str) -> SourceDocument | None:
        with connect(self.db_path) as c:
            row = c.execute("SELECT * FROM documents WHERE session_id = ? AND sha256 = ?",
                            (session_id, sha256)).fetchone()
        return _row_to_doc(row) if row else None

    def save_document(self, doc: SourceDocument, segments: list[Segment]) -> None:
        number = int(doc.document_id.split("-")[1])
        with connect(self.db_path) as c:
            c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                      "value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                      (f"doc_counter:{doc.session_id}", str(number)))
            c.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc.session_id, doc.document_id, doc.file_name, doc.extension, doc.document_type.value,
                 doc.sha256, doc.size_bytes, doc.status.value, int(doc.detected_transcript),
                 doc.stats.model_dump_json(), json.dumps([w.model_dump(mode="json") for w in doc.warnings]),
                 doc.created_at.isoformat(timespec="seconds")))
            c.executemany(
                "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(doc.session_id, s.segment_id, s.document_id, s.ordinal, s.kind.value, s.text, s.heading,
                  s.page_number, s.speaker, s.timestamp_start, s.timestamp_end, int(s.is_split_part))
                 for s in segments])

    def list_documents(self, session_id: str) -> list[SourceDocument]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM documents WHERE session_id = ? ORDER BY document_id",
                             (session_id,)).fetchall()
        return [_row_to_doc(r) for r in rows]

    def get_document(self, session_id: str, document_id: str) -> SourceDocument | None:
        with connect(self.db_path) as c:
            row = c.execute("SELECT * FROM documents WHERE session_id = ? AND document_id = ?",
                            (session_id, document_id)).fetchone()
        return _row_to_doc(row) if row else None

    def update_document(self, session_id: str, document_id: str, *,
                        status: ExtractionStatus | None = None,
                        document_type: DocumentType | None = None,
                        warnings: list[ExtractionWarning] | None = None) -> None:
        sets, params = [], []
        if status is not None:
            sets.append("status = ?"); params.append(status.value)
        if document_type is not None:
            sets.append("document_type = ?"); params.append(document_type.value)
        if warnings is not None:
            sets.append("warnings_json = ?"); params.append(json.dumps([w.model_dump(mode="json") for w in warnings]))
        if not sets:
            return
        with connect(self.db_path) as c:
            c.execute(f"UPDATE documents SET {', '.join(sets)} WHERE session_id = ? AND document_id = ?",
                      (*params, session_id, document_id))

    def delete_document(self, session_id: str, document_id: str) -> None:
        log.info("Document %s removed", document_id, extra=ctx(session_id))
        with connect(self.db_path) as c:
            c.execute("DELETE FROM segments WHERE session_id = ? AND document_id = ?", (session_id, document_id))
            c.execute("DELETE FROM documents WHERE session_id = ? AND document_id = ?", (session_id, document_id))

    # ---- segments -------------------------------------------------------
    def get_segments(self, session_id: str, document_id: str | None = None) -> list[Segment]:
        sql = "SELECT * FROM segments WHERE session_id = ?"
        params: list = [session_id]
        if document_id:
            sql += " AND document_id = ?"; params.append(document_id)
        sql += " ORDER BY document_id, ordinal"
        with connect(self.db_path) as c:
            rows = c.execute(sql, params).fetchall()
        return [Segment(segment_id=r["segment_id"], document_id=r["document_id"], ordinal=r["ordinal"],
                        kind=r["kind"], text=r["text"], heading=r["heading"], page_number=r["page_number"],
                        speaker=r["speaker"], timestamp_start=r["timestamp_start"],
                        timestamp_end=r["timestamp_end"], is_split_part=bool(r["is_split_part"]))
                for r in rows]

    # ---- run log --------------------------------------------------------
    def log(self, session_id: str | None, level: str, stage: str, message: str) -> None:
        with connect(self.db_path) as c:
            c.execute("INSERT INTO run_log(session_id, ts, level, stage, message) VALUES (?,?,?,?,?)",
                      (session_id, _now(), level, stage, message))

    def get_log(self, session_id: str | None, limit: int = 500) -> list[dict]:
        with connect(self.db_path) as c:
            if session_id:
                rows = c.execute("SELECT * FROM run_log WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                                 (session_id, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def _row_to_doc(r) -> SourceDocument:
    return SourceDocument(
        document_id=r["document_id"], session_id=r["session_id"], file_name=r["file_name"],
        extension=r["extension"], document_type=r["document_type"], sha256=r["sha256"],
        size_bytes=r["size_bytes"], status=r["status"], detected_transcript=bool(r["detected_transcript"]),
        stats=DocumentStats.model_validate_json(r["stats_json"]),
        warnings=[ExtractionWarning.model_validate(w) for w in json.loads(r["warnings_json"])],
        created_at=datetime.fromisoformat(r["created_at"]))
