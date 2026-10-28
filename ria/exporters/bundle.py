"""Collect everything an export needs for one run."""
from __future__ import annotations

from dataclasses import dataclass, field

from ria.models.analysis import (
    AnalysisRun, DocumentUnderstanding, FindingRecord, RequirementRecord, StatementRecord, StoryRecord,
    TestScenarioRecord,
)
from ria.models.enums import ACCEPTED_REVIEW, EXCLUDED_REVIEW
from ria.models.segments import Segment, SourceDocument
from ria.persistence.repositories import Repository


@dataclass
class ExportBundle:
    session_name: str
    run: AnalysisRun
    docs: dict[str, SourceDocument]
    segments: dict[str, Segment]
    statements: dict[str, StatementRecord]
    requirements: list[RequirementRecord]
    findings: list[FindingRecord]
    stories: list[StoryRecord]
    scenarios: list[TestScenarioRecord]
    understandings: dict[str, DocumentUnderstanding]
    glossary: list[tuple[str, str | None]]
    audit: list[dict] = field(default_factory=list)
    final: bool = False        # True: only APPROVED/EDITED items in the main body

    def included(self, items: list) -> list:
        """Items for the main body: accepted only (final) or everything not rejected (draft)."""
        if self.final:
            return [i for i in items if i.review_status in ACCEPTED_REVIEW]
        return [i for i in items if i.review_status not in EXCLUDED_REVIEW]

    @property
    def pending_count(self) -> int:
        items = [*self.requirements, *self.stories, *self.scenarios]
        return sum(1 for i in items if i.review_status not in ACCEPTED_REVIEW | EXCLUDED_REVIEW)


def load_bundle(repo: Repository, session_id: str, run_id: str, final: bool = False) -> ExportBundle:
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")
    session = repo.get_session(session_id) or {"name": "Untitled"}
    docs = {d.document_id: d for d in repo.list_documents(session_id) if d.document_id in run.summary.documents}
    return ExportBundle(
        session_name=session["name"], run=run, docs=docs,
        segments={s.segment_id: s for s in repo.get_segments(session_id) if s.document_id in docs},
        statements={s.statement_id: s for s in repo.get_statements(run_id)},
        requirements=repo.get_requirements(run_id), findings=repo.get_findings(run_id),
        stories=repo.get_stories(run_id), scenarios=repo.get_scenarios(run_id),
        understandings=repo.get_understandings(run_id), glossary=repo.get_glossary(session_id),
        audit=repo.get_audit(run_id), final=final)
