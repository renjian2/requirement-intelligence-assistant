"""Deterministic demo provider.

* Returns pre-written fixtures for the bundled sample files only.
* Never pretends to analyse real documents: for anything else it returns an EMPTY
  result with a clear note.
* Fixtures are per document (not per chunk). Segment references are relative
  ("S0002") and mapped to the real document ID at runtime, and statements are
  filtered to the segments the current chunk may cite - so fixtures keep working
  whatever DOC number the sample gets and whatever the chunk-size setting is.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ria.providers.base import (
    ChatMessage, LLMProvider, LLMResponse, ProviderError, ProviderStatus, RequestMeta, T, Usage,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
DEMO_BANNER = "DEMO DATA – no AI analysis performed"
_REL_SEG = re.compile(r'"(S\d{4})"')


class MockProvider(LLMProvider):
    name = "mock"
    is_demo = True
    sends_data_externally = False

    def __init__(self, fixture_dir: Path = FIXTURE_DIR):
        self.fixture_dir = fixture_dir

    # -- helpers -----------------------------------------------------------
    def _fixture_path(self, schema: type[BaseModel], meta: RequestMeta | None) -> Path | None:
        if meta is None or not meta.file_name:
            return None
        path = self.fixture_dir / schema.__name__ / f"{meta.file_name}.json"
        return path if path.exists() else None

    @staticmethod
    def _filter_to_chunk(data: dict, citable: tuple[str, ...], prompt: str = "") -> dict:
        """Keep only statements whose first cited segment belongs to this chunk. A correction whose
        target was extracted in an earlier chunk is re-pointed to that statement's STM-id, using the
        <previous_statements> list in the prompt (as a real model would)."""
        if not citable or "statements" not in data:
            return data
        allowed = set(citable)
        by_key = {st["local_key"]: st for st in data["statements"]}
        previous = []
        if "</previous_statements>" in prompt:
            body = prompt.rsplit("<previous_statements>", 1)[-1].split("</previous_statements>", 1)[0]
            previous = json.loads(body.strip() or "[]")
        kept = []
        for st in data["statements"]:
            ids = [i for i in st.get("segment_ids", []) if i in allowed]
            if not (st.get("segment_ids") and st["segment_ids"][0] in allowed):
                continue
            st = {**st, "segment_ids": ids}
            target = by_key.get(st.get("supersedes_local_key") or "")
            if target and target["segment_ids"][0] not in allowed:
                match = next((p["id"] for p in previous if target["segment_ids"][0] in p.get("segment_ids", [])), None)
                st["supersedes_local_key"] = match
            kept.append(st)
        return {**data, "statements": kept}

    def _load(self, path: Path, meta: RequestMeta) -> str:
        raw = path.read_text(encoding="utf-8")
        if meta.document_id:
            raw = _REL_SEG.sub(lambda m: f'"{meta.document_id}-{m.group(1)}"', raw)
        if meta.chunk_id:
            raw = raw.replace("{{CHUNK_ID}}", meta.chunk_id)
        return raw

    # -- interface ---------------------------------------------------------
    def generate_structured(self, messages: list[ChatMessage], schema: type[T], *,
                            temperature: float = 0.1,
                            meta: RequestMeta | None = None) -> LLMResponse[T]:
        start = time.perf_counter()
        meta = meta or RequestMeta()
        if schema.__name__ == "RequirementSynthesisResult":
            return self._rule_based_requirements(messages, schema, start)
        if schema.__name__ == "StoryGenerationResult":
            return self._rule_based_stories(messages, schema, start)
        if schema.__name__ == "IssueReviewResult":
            group = re.search(r'group = "([^"]*)"', messages[-1].content)
            raw = json.dumps({"group": group.group(1) if group else "General", "findings": []})
            return LLMResponse(parsed=schema.model_validate_json(raw), raw_text=raw, provider=self.name,
                               model="mock-fixtures", is_demo=True,
                               note=f"{DEMO_BANNER}. AI issue review is skipped in demo mode – rule checks only.")
        path = self._fixture_path(schema, meta)
        note = DEMO_BANNER
        is_context_only = bool(meta.file_name and "existing_user_stories" in meta.file_name)
        if path is not None:
            raw = json.dumps(self._filter_to_chunk(json.loads(self._load(path, meta)),
                                                   meta.citable_segment_ids, messages[-1].content if messages else ""))
            try:
                parsed = schema.model_validate_json(raw)
            except ValidationError as exc:
                raise ProviderError(f"Demo fixture {path.name} does not match {schema.__name__}: {exc}") from exc
        elif not is_context_only:
            if schema.__name__ == "StatementExtractionResult" and messages:
                raw = json.dumps(_demo_extract_statements(messages[-1].content, meta.citable_segment_ids, meta.chunk_id))
                parsed = schema.model_validate_json(raw)
                note = f"{DEMO_BANNER}. Generated rule-based test statements from document text."
            elif schema.__name__ == "DocumentUnderstanding":
                doc_name = meta.file_name or "document"
                raw = json.dumps({
                    "executive_summary": f"Overview of {doc_name}: business requirements, operational rules and workflow specifications.",
                    "actors": ["User", "Manager", "Approver", "System"],
                    "modules": ["General", "Operations", "Compliance"],
                    "glossary": [],
                    "data_entities": ["Record", "Transaction", "AuditLog"],
                    "undefined_acronyms": [],
                })
                parsed = schema.model_validate_json(raw)
                note = f"{DEMO_BANNER}. Generated demo document understanding."
            else:
                defaults = {"chunk_id": meta.chunk_id} if "chunk_id" in schema.model_fields else {}
                parsed = schema.model_validate(defaults)
                raw = parsed.model_dump_json()
                note = f"{DEMO_BANNER}. No demo fixture for this document - empty result returned."
        else:
            defaults = {"chunk_id": meta.chunk_id} if "chunk_id" in schema.model_fields else {}
            parsed = schema.model_validate(defaults)
            raw = parsed.model_dump_json()
            note = f"{DEMO_BANNER}. No demo fixture for this document - empty result returned."
        prompt_chars = sum(len(m.content) for m in messages)
        return LLMResponse(parsed=parsed, raw_text=raw, provider=self.name, model="mock-fixtures",
                           usage=Usage(prompt_chars // 4, len(raw) // 4, estimated=True),
                           latency_ms=int((time.perf_counter() - start) * 1000),
                           is_demo=True, note=note)

    def _rule_based_requirements(self, messages: list[ChatMessage], schema: type[T], start: float) -> LLMResponse[T]:
        """Demo Stage 3: one requirement per candidate statement, built by simple rules (no AI).

        Corrections absorb the statement they supersede; nothing is merged or inferred."""
        prompt = messages[-1].content
        group = (re.search(r'group = "([^"]*)"', prompt) or re.search(r'area "([^"]*)"', prompt))
        # The data block is the LAST <statements> tag (the instructions also mention the tag name).
        body = prompt.rsplit("<statements>", 1)[-1].split("</statements>", 1)[0] if "</statements>" in prompt else "[]"
        statements = json.loads(body.strip() or "[]")
        type_map = {"CONSTRAINT": "NON_FUNCTIONAL", "BUSINESS_RULE": "BUSINESS", "CORRECTION": "BUSINESS"}
        superseded_by: dict[str, list[str]] = {}
        for st in statements:
            if st.get("superseded_by"):
                superseded_by.setdefault(st["superseded_by"], []).append(st["id"])
        reqs = []
        for st in statements:
            if st.get("superseded_by") or st["type"] not in (
                    "REQUIREMENT_CANDIDATE", "BUSINESS_RULE", "CONSTRAINT", "DECISION", "CORRECTION"):
                continue
            words = st["text"].rstrip(".").split()
            reqs.append({
                "local_key": f"r{len(reqs) + 1}", "title": " ".join(words[:8]) + ("…" if len(words) > 8 else ""),
                "description": st["text"], "requirement_type": type_map.get(st["type"], "FUNCTIONAL"),
                "evidence_status": st["status"], "source_statement_ids": [st["id"], *superseded_by.get(st["id"], [])],
                "unresolved_questions": ["Demo mode: details (actor, trigger, exceptions) are added by a real AI provider."],
            })
        raw = json.dumps({"group": group.group(1) if group else "General", "requirements": reqs})
        parsed = schema.model_validate_json(raw)
        return LLMResponse(parsed=parsed, raw_text=raw, provider=self.name, model="mock-fixtures",
                           usage=Usage(len(prompt) // 4, len(raw) // 4, estimated=True),
                           latency_ms=int((time.perf_counter() - start) * 1000), is_demo=True,
                           note=f"{DEMO_BANNER}. Requirements built by simple rules (one per statement).")

    def _rule_based_stories(self, messages: list[ChatMessage], schema: type[T], start: float) -> LLMResponse[T]:
        """Demo Stage 6: one template story per requirement (no AI). Values are copied, never invented."""
        prompt = messages[-1].content
        group = re.search(r'group = "([^"]*)"', prompt)
        body = prompt.rsplit("<requirements>", 1)[-1].split("</requirements>", 1)[0]
        reqs = json.loads(body.strip() or "[]")
        roles = ("Employee", "Line Manager", "Manager", "Finance Approver", "Finance")
        stories = []
        for r in reqs:
            text = r["description"].rstrip(".")
            actor = (r.get("actors") or [None])[0] or next((x for x in roles if x.lower() in text.lower()), None)
            capability = demo_capability(text, actor)
            stories.append({
                "local_key": f"u{len(stories) + 1}", "requirement_ids": [r["id"]],
                "title": short_title(_LEADING.sub("", text)), "actor": actor, "capability": capability,
                "benefit": None,
                "acceptance_criteria": [
                    {"criterion_type": "BUSINESS_RULE", "given": "[TBC: precondition]",
                     "when": "[TBC: triggering action]", "then": "the rule is applied"}],
                "test_scenarios": [
                    {"title": "Happy path", "scenario_type": "POSITIVE", "priority": "HIGH",
                     "preconditions": ["[TBC: test data]"],
                     "steps": ["Perform the action covered by the rule", "Check the result"],
                     "expected_result": "The rule is applied"},
                    {"title": "Rule not met", "scenario_type": "NEGATIVE", "priority": "MEDIUM",
                     "preconditions": ["[TBC: test data that breaks the rule]"],
                     "steps": ["Perform the action without meeting the rule"],
                     "expected_result": "[TBC: expected error or blocking behaviour]"}],
                "clarification_questions": ["Demo mode: a real AI provider writes full stories and criteria."],
            })
        raw = json.dumps({"group": group.group(1) if group else "General", "stories": stories})
        return LLMResponse(parsed=schema.model_validate_json(raw), raw_text=raw, provider=self.name,
                           model="mock-fixtures", usage=Usage(len(prompt) // 4, len(raw) // 4, estimated=True),
                           latency_ms=int((time.perf_counter() - start) * 1000), is_demo=True,
                           note=f"{DEMO_BANNER}. Stories built from templates (one per requirement).")

    def generate_text(self, messages: list[ChatMessage], *, temperature: float = 0.2,
                      meta: RequestMeta | None = None) -> LLMResponse:
        text = f"[{DEMO_BANNER}] Narrative text will be generated by a real provider."
        return LLMResponse(parsed=None, raw_text=text, provider=self.name, model="mock-fixtures",
                           is_demo=True, note=DEMO_BANNER)

    def health_check(self) -> ProviderStatus:
        count = len(list(self.fixture_dir.rglob("*.json")))
        return ProviderStatus(ok=count > 0, provider=self.name, model="mock-fixtures", is_demo=True,
                              message=f"Demo provider ready ({count} fixtures). {DEMO_BANNER}.")

    def model_info(self) -> dict:
        return {"provider": self.name, "model": "mock-fixtures", "demo": True,
                "fixtures": sorted(str(p.relative_to(self.fixture_dir)) for p in self.fixture_dir.rglob("*.json"))}


_LEADING = re.compile(r"^(the system|the portal|system|portal)\s+(shall|must|should|will)\s+", re.I)


_VERBS = {"send", "submit", "upload", "approve", "reject", "select", "view", "see", "receive", "record", "create",
          "update", "delete", "notify", "attach", "track", "pay", "enter", "edit", "export", "download", "search"}


def demo_capability(text: str, actor: str | None) -> str:
    """Readable 'I want …' phrase from a requirement sentence (demo only).

    'The system shall send an email …' -> 'send an email …'; a statement such as 'The claim window is 45 days'
    becomes 'to ensure that the claim window is 45 days' so the narrative reads as a sentence."""
    t = _LEADING.sub("", text.strip()).rstrip(".")
    if not t:
        return "[TBC: capability]"
    t = t[:1].lower() + t[1:]
    return t if t.split()[0] in _VERBS else f"to ensure that {t}"


def short_title(capability: str, words: int = 5) -> str:
    """First words of the capability, capitalised – a short card title instead of the whole requirement."""
    parts = capability.strip().rstrip(".").split()
    title = " ".join(parts[:words]) + ("…" if len(parts) > words else "")
    return title[:1].upper() + title[1:]


def _demo_extract_statements(prompt: str, citable: tuple[str, ...], chunk_id: str = "") -> dict:
    """Extract synthetic statements from chunk segment text for non-fixture documents (demo mode)."""
    body = prompt.rsplit("<source_segments>", 1)[-1].split("</source_segments>", 1)[0] if "<source_segments>" in prompt else "[]"
    try:
        segments = json.loads(body.strip() or "[]")
    except Exception:
        segments = []
    statements = []
    citable_set = set(citable)
    for seg in segments:
        sid = seg.get("id")
        if citable_set and sid not in citable_set:
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", text) if len(s.strip()) > 15]
        if not sentences:
            sentences = [text[:150]]
        for s in sentences[:2]:
            key = f"s{len(statements) + 1}"
            st_type = "BUSINESS_RULE" if any(w in s.lower() for w in ("rule", "policy", "must", "shall", "cannot", "limit")) else "REQUIREMENT_CANDIDATE"
            statements.append({
                "local_key": key,
                "statement_type": st_type,
                "text": s,
                "evidence_status": "STATED",
                "segment_ids": [sid],
                "evidence_quote": s[:120],
                "speaker": seg.get("speaker"),
            })
    return {"chunk_id": chunk_id or "C01", "statements": statements}


def fixture_files() -> list[Path]:
    return sorted(FIXTURE_DIR.rglob("*.json"))


def load_fixture_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
