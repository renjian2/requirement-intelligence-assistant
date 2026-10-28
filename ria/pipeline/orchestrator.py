"""Phase 2 analysis pipeline.

Stage 1  Document understanding (DEEP mode only)        - one call per document
Stage 2  Statement extraction                           - one call per chunk
Stage 3  Requirement synthesis                          - one call per functional-area group
After every stage, code validates the output (citations, quotes, statuses, actors, priority).

Every call goes through `LLMCaller`, which adds caching (re-runs are free), usage accounting
and a per-call log. A fatal provider error (bad key, unknown model) stops the run; other
failures are recorded and the run continues so earlier results are never lost.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, ValidationError

from ria.config import Settings
from ria.models.analysis import (
    AnalysisRun, DocumentUnderstanding, FindingRecord, IssueReviewResult, RequirementRecord,
    RequirementSynthesisResult, RunSummary, StatementExtractionResult, StatementRecord, StoryGenerationResult,
)
from ria.models.enums import (
    DEFAULT_SOURCE_AUTHORITY, DOCUMENT_TYPE_LABELS, INPUT_MODE_LABELS, MEETING_TYPES,
    REQUIREMENT_DOC_TYPES, REQUIREMENT_SOURCE_TYPES, AnalysisMode, EvidenceStatus, ExtractionStatus,
    EXCLUDED_REVIEW, FindingType, InputMode, ReviewStatus, RunStatus, StatementType,
)
from ria.models.segments import Chunk, Segment, SourceDocument, estimate_tokens
from ria.persistence.repositories import Repository
from ria.pipeline.chunker import build_chunks
from ria.pipeline.grouping import GENERAL, build_groups
from ria.pipeline.prompts import load_prompt, render
from ria.pipeline.issues import IssueContext, merge_findings, run_rules
from ria.pipeline.validators import (
    validate_issue, validate_requirement, validate_statements, validate_story,
)
from ria.validation.confidence import requirement_confidence
from ria.providers.base import ChatMessage, LLMProvider, ProviderError, RequestMeta
from ria.providers.http_provider import ContextTooLongError, FatalProviderError
from ria.providers.schema import drop_nulls_for_defaults, extract_json
from ria.services.redaction import NoRedaction, Redactor
from ria.logging_setup import ctx, get_logger

log = get_logger(__name__)


ProgressFn = Callable[[str, float], None]
PREVIOUS_STATEMENTS = 20   # earlier transcript statements shown to the model for cross-chunk corrections


# ================================ input modes ================================

def documents_for_mode(mode: InputMode, docs: list[SourceDocument]) -> list[SourceDocument]:
    """Which confirmed documents a mode analyses. Supporting docs (policy, stories, spec) are always used."""
    confirmed = [d for d in docs if d.status == ExtractionStatus.CONFIRMED]
    if mode == InputMode.TRANSCRIPT_ONLY:
        return [d for d in confirmed if d.document_type not in REQUIREMENT_DOC_TYPES]
    if mode == InputMode.BRD_ONLY:
        return [d for d in confirmed if d.document_type not in MEETING_TYPES]
    return confirmed


def check_input_mode(mode: InputMode, docs: list[SourceDocument]) -> list[str]:
    used = documents_for_mode(mode, docs)
    has_meeting = any(d.document_type in MEETING_TYPES for d in used)
    has_req = any(d.document_type in REQUIREMENT_DOC_TYPES for d in used)
    problems = []
    if not used:
        problems.append("No confirmed documents for this mode.")
    def waiting(types: set, label: str) -> str:
        names = [f"{label} document '{d.file_name}' ({d.document_id}) is waiting for confirmation."
                 for d in docs if d.document_type in types and d.status == ExtractionStatus.EXTRACTED]
        return (" " + " ".join(names)) if names else " Upload one and confirm it."
    if mode in (InputMode.TRANSCRIPT_ONLY, InputMode.COMPARE) and not has_meeting:
        problems.append("This mode needs a confirmed meeting transcript or meeting notes." + waiting(MEETING_TYPES, "Meeting"))
    if mode in (InputMode.BRD_ONLY, InputMode.COMPARE) and not has_req:
        problems.append("This mode needs a confirmed BRD or requirement document." + waiting(REQUIREMENT_DOC_TYPES, "BRD"))
    return problems


def suggest_input_mode(docs: list[SourceDocument]) -> InputMode:
    confirmed = [d for d in docs if d.status == ExtractionStatus.CONFIRMED] or docs
    has_meeting = any(d.document_type in MEETING_TYPES for d in confirmed)
    has_req = any(d.document_type in REQUIREMENT_DOC_TYPES for d in confirmed)
    if has_meeting and has_req:
        return InputMode.COMPARE
    return InputMode.TRANSCRIPT_ONLY if has_meeting else InputMode.BRD_ONLY


# ================================ estimates ================================

@dataclass
class RunEstimate:
    calls: int
    input_tokens: int
    output_tokens: int
    per_stage: dict[str, int]


def estimate_run(docs: list[SourceDocument], chunks: dict[str, list[Chunk]], mode: AnalysisMode,
                 settings: Settings, find_issues: bool = True, stories: bool = True) -> RunEstimate:
    system = estimate_tokens(load_prompt("system")[0])
    calls, tin, tout, per = 0, 0, 0, {}
    if mode == AnalysisMode.DEEP:
        per["Document understanding"] = len(docs)
        for d in docs:
            tin += system + 250 + min(d.stats.estimated_tokens, int(settings.chunk_token_budget * 1.5))
            tout += 350
    n_chunks = sum(len(c) for c in chunks.values())
    per["Statement extraction"] = n_chunks
    seg_tokens = 0
    for cs in chunks.values():
        for c in cs:
            tin += system + 650 + c.estimated_tokens
            seg_tokens += c.estimated_tokens
    tout += int(seg_tokens * 1.1) + 150 * n_chunks
    est_statements = int(sum(d.stats.segments for d in docs) * 0.8)
    est_candidates = int(est_statements * 0.6)
    groups = max(1, math.ceil(est_candidates / settings.max_group_statements)) + (3 if mode == AnalysisMode.DEEP else 0)
    per["Requirement synthesis"] = groups
    tin += groups * (system + 700) + est_candidates * 90
    tout += est_candidates * 160
    est_reqs = max(1, int(est_candidates * 0.8))
    req_groups = max(1, math.ceil(est_reqs / 8))
    if find_issues and mode == AnalysisMode.DEEP:
        per["Issue review"] = req_groups
        tin += req_groups * (system + 900) + est_reqs * 160
        tout += req_groups * 700
    if stories:
        per["Stories & tests"] = req_groups
        tin += req_groups * (system + 900) + est_reqs * 160
        tout += est_reqs * 450
    calls = sum(per.values())
    return RunEstimate(calls, tin, tout, per)


# ================================ LLM caller ================================

class LLMCaller:
    """Provider wrapper adding cache, usage accounting and call logging."""

    def __init__(self, provider: LLMProvider, repo: Repository, run: AnalysisRun, use_cache: bool = True):
        self.provider, self.repo, self.run = provider, repo, run
        self.use_cache = use_cache and not provider.is_demo
        self.model = provider.model_info().get("model", "")
        self._lock = threading.Lock()   # summary counters are shared by parallel calls

    def _key(self, messages: list[ChatMessage], schema: type[BaseModel], prompt_version: str) -> str:
        payload = json.dumps({"p": self.provider.name, "m": self.model, "v": prompt_version, "s": schema.__name__,
                              "schema": schema.model_json_schema(),
                              "msgs": [m.__dict__ for m in messages]}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def call(self, *, stage: str, ref: str, messages: list[ChatMessage], schema: type[BaseModel],
             meta: RequestMeta, prompt_version: str):
        s = self.run.summary
        key = self._key(messages, schema, prompt_version) if self.use_cache else None
        if key and (hit := self.repo.cache_get(key)):
            try:
                parsed = schema.model_validate(drop_nulls_for_defaults(extract_json(hit["raw_text"]), schema))
                with self._lock:
                    s.calls += 1; s.cached_calls += 1
                self.repo.log_llm_call(run_id=self.run.run_id, session_id=self.run.session_id, stage=stage, ref=ref,
                                       provider=self.provider.name, model=self.model, prompt_version=prompt_version,
                                       input_tokens=0, output_tokens=0, estimated=False, latency_ms=0, cached=True,
                                       status="OK", message="Served from cache (no tokens used)")
                log.debug("AI call %s/%s served from cache", stage, ref,
                          extra=ctx(self.run.session_id, self.run.run_id))
                return parsed, ""
            except (ValueError, ValidationError):
                pass  # stale cache entry - call the model again
        start = time.perf_counter()
        try:
            resp = self.provider.generate_structured(messages, schema, temperature=0.1, meta=meta)
        except ProviderError as exc:
            with self._lock:
                s.calls += 1; s.failed_calls += 1
            self.repo.log_llm_call(run_id=self.run.run_id, session_id=self.run.session_id, stage=stage, ref=ref,
                                   provider=self.provider.name, model=self.model, prompt_version=prompt_version,
                                   input_tokens=0, output_tokens=0, estimated=True,
                                   latency_ms=int((time.perf_counter() - start) * 1000), cached=False,
                                   status="ERROR", message=str(exc))
            log.warning("AI call %s/%s failed after %d ms: %s", stage, ref,
                        int((time.perf_counter() - start) * 1000), exc, extra=ctx(self.run.session_id, self.run.run_id))
            raise
        with self._lock:
            s.calls += 1
            s.input_tokens += resp.usage.input_tokens
            s.output_tokens += resp.usage.output_tokens
            s.tokens_estimated = s.tokens_estimated or resp.usage.estimated
        if key:
            self.repo.cache_put(key, self.provider.name, self.model, resp.parsed.model_dump_json(),
                                resp.usage.input_tokens, resp.usage.output_tokens)
        self.repo.log_llm_call(run_id=self.run.run_id, session_id=self.run.session_id, stage=stage, ref=ref,
                               provider=self.provider.name, model=resp.model, prompt_version=prompt_version,
                               input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                               estimated=resp.usage.estimated, latency_ms=resp.latency_ms, cached=False,
                               status="OK", message=resp.note)
        log.info("AI call %s/%s OK: %s/%s %d ms, tokens %d in + %d out%s", stage, ref, self.provider.name, resp.model,
                 resp.latency_ms, resp.usage.input_tokens, resp.usage.output_tokens,
                 " (estimated)" if resp.usage.estimated else "", extra=ctx(self.run.session_id, self.run.run_id))
        return resp.parsed, resp.note


# ================================ orchestrator ================================

@dataclass
class RunOptions:
    input_mode: InputMode
    analysis_mode: AnalysisMode = AnalysisMode.DEEP
    redact: bool = False
    redact_names: bool = True
    use_cache: bool = True
    find_issues: bool = True
    generate_stories: bool = True
    max_tests_per_story: int = 3


@dataclass
class _Context:
    run: AnalysisRun
    docs: list[SourceDocument]
    segments: dict[str, Segment] = field(default_factory=dict)
    sent_text: dict[str, str] = field(default_factory=dict)      # what the model saw (maybe redacted)
    understandings: dict[str, DocumentUnderstanding] = field(default_factory=dict)
    statements: list[StatementRecord] = field(default_factory=list)
    requirements: list[RequirementRecord] = field(default_factory=list)
    findings: list[FindingRecord] = field(default_factory=list)


def _segment_payload(seg: Segment, redactor: Redactor) -> dict:
    d = {"id": seg.segment_id, "text": redactor.redact(seg.text)}
    if seg.speaker:
        d["speaker"] = redactor.redact(seg.speaker)
    if seg.timestamp_start:
        d["time"] = seg.timestamp_start
    if seg.heading:
        d["heading"] = seg.heading
    if seg.page_number is not None:
        d["page"] = seg.page_number
    return d


class AnalysisOrchestrator:
    def __init__(self, repo: Repository, settings: Settings, provider: LLMProvider):
        self.repo, self.settings, self.provider = repo, settings, provider

    # ================================================================ public API
    def run(self, session_id: str, options: RunOptions, progress: ProgressFn | None = None) -> AnalysisRun:
        progress = progress or (lambda *_: None)
        all_docs = self.repo.list_documents(session_id)
        problems = check_input_mode(options.input_mode, all_docs)
        if problems:
            raise ValueError(" ".join(problems))
        docs = documents_for_mode(options.input_mode, all_docs)
        info = self.provider.model_info()
        run = AnalysisRun(run_id=uuid.uuid4().hex[:10], session_id=session_id, provider=self.provider.name,
                          model=str(info.get("model", "")), analysis_mode=options.analysis_mode.value,
                          input_mode=options.input_mode, redaction=options.redact, is_demo=self.provider.is_demo,
                          max_tests_per_story=options.max_tests_per_story,
                          summary=RunSummary(documents=[d.document_id for d in docs],
                                             document_types={d.document_id: d.document_type.value for d in docs}))
        self.repo.save_run(run)
        self.repo.set_input_mode(session_id, options.input_mode)
        self.repo.log(session_id, "INFO", "analysis",
                      f"Run {run.run_id} started: {self.provider.name}/{run.model}, {options.analysis_mode.value}, "
                      f"{options.input_mode.value}, {len(docs)} document(s), redaction={'on' if options.redact else 'off'}")
        log.info("Analysis started: provider=%s model=%s depth=%s mode=%s documents=%d redaction=%s",
                 self.provider.name, run.model, options.analysis_mode.value, options.input_mode.value, len(docs),
                 options.redact, extra={"session": session_id, "run": run.run_id})
        ctx, redactor = self._new_context(run, docs, options)
        caller = LLMCaller(self.provider, self.repo, run, options.use_cache)

        def stages() -> None:
            if options.analysis_mode == AnalysisMode.DEEP:
                self._stage_understanding(ctx, caller, redactor, progress)
                run.summary.stages.append("understanding")
            self._stage_statements(ctx, caller, redactor, options, progress)
            run.summary.stages.append("statements")
            self._stage_requirements(ctx, caller, redactor, options, progress)
            run.summary.stages.append("requirements")
            if options.find_issues:
                self._stage_issues(ctx, caller, redactor, options, progress)
                run.summary.stages.append("issues")
            if options.generate_stories:
                self._stage_stories(ctx, caller, redactor, options, progress)
                run.summary.stages.append("stories")
        return self._execute(run, stages, progress)

    def continue_run(self, run_id: str, *, find_issues: bool, generate_stories: bool,
                     max_tests_per_story: int | None = None, use_cache: bool = True,
                     progress: ProgressFn | None = None) -> AnalysisRun:
        """Re-run the issue and/or story stages of an existing run using the CURRENT (reviewed)
        requirements: rejected / out-of-scope requirements are skipped, edited text is used."""
        progress = progress or (lambda *_: None)
        run = self.repo.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        docs = [d for d in self.repo.list_documents(run.session_id) if d.document_id in run.summary.documents]
        options = RunOptions(input_mode=run.input_mode, analysis_mode=AnalysisMode(run.analysis_mode),
                             redact=run.redaction, use_cache=use_cache,
                             max_tests_per_story=max_tests_per_story or run.max_tests_per_story)
        run.max_tests_per_story = options.max_tests_per_story
        run.status = RunStatus.RUNNING
        run.summary.errors = []
        ctx, redactor = self._new_context(run, docs, options)
        ctx.understandings = self.repo.get_understandings(run_id)
        ctx.statements = self.repo.get_statements(run_id)
        ctx.requirements = self.repo.get_requirements(run_id)
        ctx.findings = self.repo.get_findings(run_id)
        caller = LLMCaller(self.provider, self.repo, run, use_cache)
        self.repo.log(run.session_id, "INFO", "analysis",
                      f"Run {run_id} continued with {self.provider.name}: issues={find_issues}, stories={generate_stories}")

        def stages() -> None:
            if find_issues:
                self._stage_issues(ctx, caller, redactor, options, progress)
            if generate_stories:
                self._stage_stories(ctx, caller, redactor, options, progress)
            for st in (["issues"] if find_issues else []) + (["stories"] if generate_stories else []):
                if st not in run.summary.stages:
                    run.summary.stages.append(st)
        return self._execute(run, stages, progress)

    # ================================================================ helpers
    def _new_context(self, run: AnalysisRun, docs: list[SourceDocument], options: RunOptions):
        ctx = _Context(run=run, docs=docs)
        redactor: Redactor = Redactor(redact_names=options.redact_names) if options.redact else NoRedaction()
        for d in docs:
            for seg in self.repo.get_segments(run.session_id, d.document_id):
                ctx.segments[seg.segment_id] = seg
        redactor.register_speakers(sorted({s.speaker for s in ctx.segments.values() if s.speaker}))
        ctx.sent_text = {sid: redactor.redact(s.text) or "" for sid, s in ctx.segments.items()}
        return ctx, redactor

    def _execute(self, run: AnalysisRun, stages: Callable[[], None], progress: ProgressFn) -> AnalysisRun:
        session_id = run.session_id
        started = time.perf_counter()
        try:
            stages()
            run.status = RunStatus.COMPLETED_WITH_ERRORS if (run.summary.failed_calls or run.summary.errors) \
                else RunStatus.COMPLETED
        except FatalProviderError as exc:
            run.status = RunStatus.FAILED
            run.summary.errors.append(str(exc))
            self.repo.log(session_id, "ERROR", "analysis", f"Run {run.run_id} stopped: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep partial results, report the problem
            run.status = RunStatus.FAILED
            run.summary.errors.append(f"Unexpected error: {type(exc).__name__}: {exc}")
            log.exception("Analysis crashed", extra={"session": session_id, "run": run.run_id})
            self.repo.log(session_id, "ERROR", "analysis", f"Run {run.run_id} crashed: {type(exc).__name__}: {exc}")
        run.summary.duration_s = round(run.summary.duration_s + time.perf_counter() - started, 1)
        run.finished_at = datetime.now(timezone.utc)
        self.repo.save_run(run)
        s = run.summary
        self.repo.log(session_id, "INFO" if run.status != RunStatus.FAILED else "ERROR", "analysis",
                      f"Run {run.run_id} {run.status.value}: {s.statements} statements, {s.requirements} requirements, "
                      f"{s.findings} findings, {s.stories} stories, {s.scenarios} test scenarios, {s.calls} calls "
                      f"({s.cached_calls} cached, {s.failed_calls} failed), tokens: {s.input_tokens:,} prompt + "
                      f"{s.output_tokens:,} completion")
        (log.error if run.status == RunStatus.FAILED else log.info)(
            "Analysis %s in %.1fs: %d statements, %d requirements, %d findings, %d stories, %d tests; %d calls "
            "(%d cached, %d failed); tokens %d in + %d out; errors=%d", run.status.value, s.duration_s, s.statements,
            s.requirements, s.findings, s.stories, s.scenarios, s.calls, s.cached_calls, s.failed_calls,
            s.input_tokens, s.output_tokens, len(s.errors), extra={"session": session_id, "run": run.run_id})
        progress("Done", 1.0)
        return run

    def _parallel(self, items: list, fn: Callable, progress: ProgressFn, label: Callable[[object], str],
                  start: float, span: float) -> list:
        """Run fn over items (in parallel if RIA_MAX_PARALLEL_CALLS > 1). Results keep input order.
        Progress is reported from the calling thread (Streamlit cannot update from worker threads)."""
        n = max(1, self.settings.max_parallel_calls)
        total = max(1, len(items))
        if n == 1 or len(items) <= 1:
            out = []
            for i, item in enumerate(items):
                progress(label(item), start + span * i / total)
                out.append(fn(item))
            return out
        results: list = [None] * len(items)
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
            for done, fut in enumerate(as_completed(futures), start=1):
                i = futures[fut]
                results[i] = fut.result()   # FatalProviderError propagates and stops the run
                progress(label(items[i]), start + span * done / total)
        return results

    def _requirement_payload(self, r: RequirementRecord, statements: dict[str, StatementRecord],
                             redactor: Redactor, docs: dict[str, SourceDocument]) -> dict:
        quotes = [redactor.redact(statements[s].evidence_quote) for s in r.source_statement_ids if s in statements][:3]
        d = {"id": r.requirement_id, "title": redactor.redact(r.title), "description": redactor.redact(r.description),
             "type": r.requirement_type.value, "evidence_status": r.evidence_status.value,
             "sources": sorted({docs[x].document_type.value for x in r.document_ids if x in docs}),
             "quotes": quotes}
        for key, val in (("actors", r.actors), ("trigger", r.trigger), ("expected_behavior", r.expected_behavior),
                         ("preconditions", r.preconditions), ("exceptions", r.exceptions),
                         ("business_rules", r.business_rules), ("open_questions", r.unresolved_questions)):
            if val:
                d[key] = [redactor.redact(v) for v in val] if isinstance(val, list) else redactor.redact(val)
        return d

    # ---------------------------------------------------------------- stage 1
    def _stage_understanding(self, ctx: _Context, caller: LLMCaller, redactor: Redactor, progress: ProgressFn) -> None:
        system, _ = load_prompt("system")
        budget = int(self.settings.chunk_token_budget * 1.5)
        for i, doc in enumerate(ctx.docs):
            progress(f"Stage 1 · understanding {doc.file_name}", 0.02 + 0.12 * i / max(1, len(ctx.docs)))
            segs = [s for s in ctx.segments.values() if s.document_id == doc.document_id]
            chosen, used, digest = [], 0, False
            for s in segs:  # long documents: keep headings' first segments within budget
                t = estimate_tokens(s.text) + 12
                if used + t > budget:
                    digest = True
                    continue
                chosen.append(s); used += t
            prompt, version = render(
                "document_understanding", file_name=doc.file_name,
                document_type=DOCUMENT_TYPE_LABELS[doc.document_type],
                digest_note="NOTE: only part of a long document is shown." if digest else "",
                segments_json=json.dumps([_segment_payload(s, redactor) for s in chosen], ensure_ascii=False))
            try:
                parsed, _ = caller.call(stage="understanding", ref=doc.document_id,
                                        messages=[ChatMessage("system", system), ChatMessage("user", prompt)],
                                        schema=DocumentUnderstanding, prompt_version=version,
                                        meta=RequestMeta(stage="understanding", document_id=doc.document_id,
                                                         file_name=doc.file_name, prompt_version=version))
            except FatalProviderError:
                raise
            except ProviderError as exc:
                ctx.run.summary.errors.append(f"{doc.document_id} understanding: {exc}")
                continue
            ctx.understandings[doc.document_id] = parsed
            self.repo.save_understanding(ctx.run.run_id, doc.document_id, parsed)
        added = auto_glossary(self.repo, ctx.run.session_id, ctx.understandings, ctx.docs)
        if added:
            log.info("Automatic glossary: %d term(s) added", added, extra={"session": ctx.run.session_id,
                                                                            "run": ctx.run.run_id})

    # ---------------------------------------------------------------- stage 2
    def _stage_statements(self, ctx: _Context, caller: LLMCaller, redactor: Redactor, options: RunOptions,
                          progress: ProgressFn) -> None:
        system, _ = load_prompt("system")
        glossary = self.repo.get_glossary(ctx.run.session_id)
        corrections = self.repo.accepted_corrections(ctx.run.session_id)
        all_chunks = [(d, c) for d in ctx.docs for c in build_chunks(
            d.document_id, [s for s in ctx.segments.values() if s.document_id == d.document_id],
            self.settings.chunk_token_budget, d.detected_transcript, self.settings.transcript_overlap_segments)]
        for i, (doc, chunk) in enumerate(all_chunks):
            progress(f"Stage 2 · statements {chunk.chunk_id}", 0.15 + 0.4 * i / max(1, len(all_chunks)))
            context_block = self._context_block(doc, ctx, glossary, corrections)
            self._extract_chunk(ctx, caller, redactor, system, doc, chunk.chunk_id, chunk.segment_ids,
                                chunk.context_segment_ids, context_block, options, corrections)
        self.repo.save_statements(ctx.statements)
        s = ctx.run.summary
        s.statements = len(ctx.statements)
        s.statements_verified = sum(1 for st in ctx.statements if st.quote_verified)
        self.repo.save_run(ctx.run)

    def _context_block(self, doc: SourceDocument, ctx: _Context, glossary, corrections) -> str:
        lines = []
        if glossary:
            lines.append("PROJECT GLOSSARY: " + "; ".join(f"{t} = {m}" if m else t for t, m in glossary))
        if corrections and (doc.document_type in MEETING_TYPES or doc.detected_transcript):
            lines.append("KNOWN TRANSCRIPTION ERRORS (original → intended). Quotes must keep the ORIGINAL word; "
                         "mention the intended word in transcription_note: "
                         + "; ".join(f"{o} → {n}" for o, n in corrections))
        u = ctx.understandings.get(doc.document_id)
        if u:
            if u.actors:
                lines.append("KNOWN ACTORS: " + ", ".join(u.actors))
            if u.modules:
                lines.append("FUNCTIONAL AREAS: " + ", ".join(u.modules))
        return ("\n".join(lines) + "\n") if lines else ""

    def _extract_chunk(self, ctx: _Context, caller: LLMCaller, redactor: Redactor, system: str,
                       doc: SourceDocument, chunk_id: str, seg_ids: list[str], context_ids: list[str],
                       context_block: str, options: RunOptions, corrections) -> None:
        prompt, version = render(
            "statement_extraction", file_name=doc.file_name, document_type=DOCUMENT_TYPE_LABELS[doc.document_type],
            authority=f"{DEFAULT_SOURCE_AUTHORITY[doc.document_type]}/10",
            input_mode=INPUT_MODE_LABELS[options.input_mode], context_block=context_block, chunk_id=chunk_id,
            previous_json="{previous_json}",
            context_json=json.dumps([_segment_payload(ctx.segments[s], redactor) for s in context_ids],
                                    ensure_ascii=False),
            segments_json=json.dumps([_segment_payload(ctx.segments[s], redactor) for s in seg_ids],
                                     ensure_ascii=False))
        previous = []
        if doc.document_type in MEETING_TYPES or doc.detected_transcript:
            previous = [{"id": s.statement_id, "type": s.statement_type.value, "text": redactor.redact(s.text),
                         "segment_ids": s.segment_ids}
                        for s in ctx.statements if s.document_id == doc.document_id
                        and s.statement_type != StatementType.CONTEXT][-PREVIOUS_STATEMENTS:]
        prompt = prompt.replace("{previous_json}", json.dumps(previous, ensure_ascii=False))
        meta = RequestMeta(stage="statements", document_id=doc.document_id, file_name=doc.file_name,
                           chunk_id=chunk_id, citable_segment_ids=tuple(seg_ids), prompt_version=version)
        try:
            parsed, _ = caller.call(stage="statements", ref=chunk_id, schema=StatementExtractionResult,
                                    messages=[ChatMessage("system", system), ChatMessage("user", prompt)],
                                    meta=meta, prompt_version=version)
        except ContextTooLongError:
            if len(seg_ids) <= 1:
                ctx.run.summary.errors.append(f"{chunk_id}: a single segment is too long for the model")
                return
            half = len(seg_ids) // 2
            self.repo.log(ctx.run.session_id, "WARNING", "analysis", f"{chunk_id} too long – splitting in two")
            with caller._lock:
                ctx.run.summary.failed_calls = max(0, ctx.run.summary.failed_calls - 1)
            self._extract_chunk(ctx, caller, redactor, system, doc, f"{chunk_id}a", seg_ids[:half], context_ids,
                                context_block, options, corrections)
            self._extract_chunk(ctx, caller, redactor, system, doc, f"{chunk_id}b", seg_ids[half:],
                                seg_ids[max(0, half - 2):half] if doc.detected_transcript else [],
                                context_block, options, corrections)
            return
        except FatalProviderError:
            raise
        except ProviderError as exc:
            ctx.run.summary.errors.append(f"{chunk_id}: {exc}")
            return
        records = validate_statements(
            parsed.statements, run_id=ctx.run.run_id, chunk_id=chunk_id, document_id=doc.document_id,
            document_type=doc.document_type, citable_ids=seg_ids, sent_text=ctx.sent_text, segments=ctx.segments,
            start_number=len(ctx.statements) + 1, redactor=redactor, threshold=self.settings.evidence_threshold,
            corrections=corrections, earlier={s.statement_id: s for s in ctx.statements})
        ctx.statements.extend(records)

    # ---------------------------------------------------------------- stage 3
    def _stage_requirements(self, ctx: _Context, caller: LLMCaller, redactor: Redactor, options: RunOptions,
                            progress: ProgressFn) -> None:
        system, _ = load_prompt("system")
        by_id = {s.statement_id: s for s in ctx.statements}
        candidates = [s for s in ctx.statements if s.statement_type in REQUIREMENT_SOURCE_TYPES
                      and s.evidence_status != EvidenceStatus.UNSUPPORTED]
        modules = [m for u in ctx.understandings.values() for m in u.modules]
        actors = list(dict.fromkeys(a for u in ctx.understandings.values() for a in u.actors))
        glossary = self.repo.get_glossary(ctx.run.session_id)
        groups = list(build_groups(candidates, by_id, modules, self.settings.max_group_statements).items())

        def work(item):
            group, stmts = item
            payload = [{
                "id": s.statement_id, "type": s.statement_type.value, "status": s.evidence_status.value,
                "text": redactor.redact(s.text), "quote": redactor.redact(s.evidence_quote),
                "document_type": s.document_type.value,
                **({"speaker": redactor.redact(s.speaker)} if s.speaker else {}),
                **({"superseded_by": s.superseded_by_id} if s.superseded_by_id else {}),
                **({"transcription_note": s.transcription_note} if s.possible_transcription_issue else {}),
            } for s in stmts]
            prompt, version = render(
                "requirement_synthesis", group=group, input_mode=INPUT_MODE_LABELS[options.input_mode],
                actors=", ".join(actors) or "(none identified)", glossary_block=_glossary_block(glossary),
                statements_json=json.dumps(payload, ensure_ascii=False))
            try:
                parsed, _ = caller.call(stage="requirements", ref=group, schema=RequirementSynthesisResult,
                                        messages=[ChatMessage("system", system), ChatMessage("user", prompt)],
                                        prompt_version=version,
                                        meta=RequestMeta(stage="requirements", prompt_version=version))
            except FatalProviderError:
                raise
            except ProviderError as exc:
                ctx.run.summary.errors.append(f"Requirements '{group}': {exc}")
                return []
            group_map = {s.statement_id: s for s in stmts}
            return [validate_requirement(d, run_id=ctx.run.run_id, group=group, group_statements=group_map,
                                         segments=ctx.segments, known_actors=actors, redactor=redactor)
                    for d in parsed.requirements]

        results = self._parallel(groups, work, progress, lambda g: f"Stage 3 · requirements: {g[0]}", 0.55, 0.2)
        requirements = [r for batch in results for r in batch]
        for n, r in enumerate(requirements, start=1):
            r.requirement_id = f"REQ-{n:03d}"
        self.repo.save_requirements(requirements)
        ctx.requirements = requirements
        ctx.run.summary.requirements = len(requirements)
        self.repo.save_run(ctx.run)

    # ---------------------------------------------------------------- stage 4/5
    def _stage_issues(self, ctx: _Context, caller: LLMCaller, redactor: Redactor, options: RunOptions,
                      progress: ProgressFn) -> None:
        """Rules (always) + LLM review per functional area (Deep mode). Contradictions mark the
        linked requirements CONFLICTING and lower their confidence."""
        progress("Stage 4 · checking rules (contradictions, gaps, vague wording)", 0.76)
        session_id = ctx.run.session_id
        glossary = self.repo.get_glossary(session_id)
        stmts = {s.statement_id: s for s in ctx.statements}
        reqs = [r for r in ctx.requirements if r.review_status not in EXCLUDED_REVIEW]
        icx = IssueContext(run_id=ctx.run.run_id, docs={d.document_id: d for d in ctx.docs}, statements=stmts,
                           requirements=reqs, understandings=ctx.understandings, project_glossary=glossary,
                           accepted_corrections=self.repo.accepted_corrections(session_id))
        rule_findings = run_rules(icx)
        llm_findings: list[FindingRecord] = []
        if options.analysis_mode == AnalysisMode.DEEP:
            system, _ = load_prompt("system")
            actors = list(dict.fromkeys(a for u in ctx.understandings.values() for a in u.actors))
            docs = {d.document_id: d for d in ctx.docs}
            groups: dict[str, list[RequirementRecord]] = {}
            for r in reqs:
                if r.evidence_status != EvidenceStatus.UNSUPPORTED:
                    groups.setdefault(r.group.split(" (part")[0], []).append(r)

            def work(item):
                group, items = item
                ids = {r.requirement_id for r in items}
                known = [f"- {f.finding_type.value}: {f.title}" for f in rule_findings if ids & set(f.requirement_ids)]
                prompt, version = render(
                    "issue_review", group=group, input_mode=INPUT_MODE_LABELS[options.input_mode],
                    actors=", ".join(actors) or "(none identified)", glossary_block=_glossary_block(glossary),
                    known_findings="\n".join(known) or "(none)",
                    requirements_json=json.dumps([self._requirement_payload(r, stmts, redactor, docs) for r in items],
                                                 ensure_ascii=False))
                try:
                    parsed, _ = caller.call(stage="issues", ref=group, schema=IssueReviewResult,
                                            messages=[ChatMessage("system", system), ChatMessage("user", prompt)],
                                            prompt_version=version,
                                            meta=RequestMeta(stage="issues", prompt_version=version))
                except FatalProviderError:
                    raise
                except ProviderError as exc:
                    ctx.run.summary.errors.append(f"Issue review '{group}': {exc}")
                    return []
                group_reqs = {r.requirement_id: r for r in items}
                out = [validate_issue(d, run_id=ctx.run.run_id, group_reqs=group_reqs, redactor=redactor)
                       for d in parsed.findings[:8]]
                return [f for f in out if f]

            results = self._parallel(list(groups.items()), work, progress,
                                     lambda g: f"Stage 5 · AI issue review: {g[0]}", 0.78, 0.07)
            llm_findings = [f for batch in results for f in batch]
        findings = merge_findings(rule_findings, llm_findings)
        # Contradictions change the evidence status of the linked requirements.
        by_req = {r.requirement_id: r for r in ctx.requirements}
        for f in findings:
            if f.finding_type != FindingType.CONTRADICTION:
                continue
            for rid in f.requirement_ids:
                r = by_req.get(rid)
                if r is None:
                    continue
                note = f"Conflicts with other sources – see {f.finding_id}"
                if r.evidence_status != EvidenceStatus.UNSUPPORTED:
                    r.evidence_status = EvidenceStatus.CONFLICTING
                if note not in r.validation_notes:
                    r.validation_notes.append(note)
                b = requirement_confidence(r, [stmts[s] for s in r.source_statement_ids if s in stmts])
                r.confidence_breakdown, r.confidence = b, b.total
        self.repo.save_requirements(ctx.requirements)
        self.repo.replace_items("findings", ctx.run.run_id, findings)
        ctx.findings = findings
        ctx.run.summary.findings = len(findings)
        self.repo.save_run(ctx.run)

    # ---------------------------------------------------------------- stage 6
    def _stage_stories(self, ctx: _Context, caller: LLMCaller, redactor: Redactor, options: RunOptions,
                       progress: ProgressFn) -> None:
        system, _ = load_prompt("system")
        glossary = self.repo.get_glossary(ctx.run.session_id)
        stmts = {s.statement_id: s for s in ctx.statements}
        docs = {d.document_id: d for d in ctx.docs}
        actors = list(dict.fromkeys([a for u in ctx.understandings.values() for a in u.actors]
                                    + [a for r in ctx.requirements for a in r.actors]))
        eligible = [r for r in ctx.requirements if r.review_status not in EXCLUDED_REVIEW
                    and r.evidence_status not in (EvidenceStatus.UNSUPPORTED, EvidenceStatus.PROPOSED)]
        groups: dict[str, list[RequirementRecord]] = {}
        for r in eligible:
            groups.setdefault(r.group.split(" (part")[0], []).append(r)
        # Large areas are split so each call stays small.
        batches = [(g, items[i:i + 8]) for g, items in groups.items() for i in range(0, len(items), 8)]
        open_findings = [f for f in ctx.findings if f.review_status not in (ReviewStatus.REJECTED, ReviewStatus.RESOLVED)]

        def work(item):
            group, items = item
            ids = {r.requirement_id for r in items}
            questions = [{"requirements": f.requirement_ids, "question": redactor.redact(f.clarification_question)}
                         for f in open_findings if ids & set(f.requirement_ids) and f.clarification_question]
            prompt, version = render(
                "story_generation", group=group, actors=", ".join(actors) or "(none identified)",
                max_tests=str(options.max_tests_per_story), glossary_block=_glossary_block(glossary),
                open_questions_json=json.dumps(questions, ensure_ascii=False),
                requirements_json=json.dumps([self._requirement_payload(r, stmts, redactor, docs) for r in items],
                                             ensure_ascii=False))
            try:
                parsed, _ = caller.call(stage="stories", ref=group, schema=StoryGenerationResult,
                                        messages=[ChatMessage("system", system), ChatMessage("user", prompt)],
                                        prompt_version=version, meta=RequestMeta(stage="stories", prompt_version=version))
            except FatalProviderError:
                raise
            except ProviderError as exc:
                ctx.run.summary.errors.append(f"Stories '{group}': {exc}")
                return []
            group_reqs = {r.requirement_id: r for r in items}
            out = []
            for d in parsed.stories:
                res = validate_story(d, run_id=ctx.run.run_id, group=group, group_reqs=group_reqs, statements=stmts,
                                     known_actors=actors, redactor=redactor, max_tests=options.max_tests_per_story)
                if res:
                    out.append(res)
            return out

        results = self._parallel(batches, work, progress, lambda b: f"Stage 6 · stories & tests: {b[0]}", 0.86, 0.12)
        stories, scenarios = [], []
        for batch in results:
            for story, tests in batch:
                story.story_id = f"US-{len(stories) + 1:03d}"
                for k, ac in enumerate(story.acceptance_criteria, start=1):
                    ac.ac_id = f"AC-{len(stories) + 1:03d}-{k:02d}"
                for t in tests:
                    t.story_id = story.story_id
                    t.scenario_id = f"TS-{len(scenarios) + 1:03d}"
                    scenarios.append(t)
                stories.append(story)
        self.repo.replace_items("stories", ctx.run.run_id, stories)
        self.repo.replace_items("scenarios", ctx.run.run_id, scenarios)
        ctx.run.summary.stories, ctx.run.summary.scenarios = len(stories), len(scenarios)
        ctx.run.summary.stories_audit_mark = self.repo.last_audit_id(ctx.run.run_id)
        self.repo.save_run(ctx.run)


def auto_glossary(repo: Repository, session_id: str, understandings: dict[str, DocumentUnderstanding],
                  docs: list[SourceDocument]) -> int:
    """Build the project glossary automatically from the Deep document overview.

    Terms are collected from every document; a meaning is kept only when a document defines it, and
    definitions from authoritative documents (BRD, policy, requirement doc) win. Terms the user (or an
    earlier run) already defined are never overwritten. Returns the number of new or newly defined terms."""
    authority = {d.document_id: DEFAULT_SOURCE_AUTHORITY[d.document_type] for d in docs}
    found: dict[str, tuple[str, str | None, int]] = {}
    for doc_id, u in understandings.items():
        rank = authority.get(doc_id, 0)
        for g in u.glossary:
            term = g.term.strip()
            if not term:
                continue
            meaning = g.meaning.strip() if g.defined_in_source and g.meaning else None
            key = term.casefold()
            old = found.get(key)
            if old is None or (meaning and (old[1] is None or rank > old[2])):
                found[key] = (term, meaning, rank)
    existing = {t.casefold(): (t, m) for t, m in repo.get_glossary(session_id)}
    merged = dict(existing)
    changed = 0
    for key, (term, meaning, _) in found.items():
        if key not in merged:
            merged[key] = (term, meaning); changed += 1
        elif meaning and not merged[key][1]:
            merged[key] = (merged[key][0], meaning); changed += 1
    if changed:
        repo.replace_glossary(session_id, list(merged.values()))
    return changed


def _glossary_block(glossary: list[tuple[str, str | None]]) -> str:
    if not glossary:
        return ""
    return "PROJECT GLOSSARY: " + "; ".join(f"{t} = {m}" if m else t for t, m in glossary) + "\n"
