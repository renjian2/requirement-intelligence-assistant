"""Evaluate the pipeline against the issues planted in sample_data/expected_findings.json.

Usage:
    python -m eval.run_eval                      # demo provider (no API key)
    python -m eval.run_eval --provider grok      # uses .env settings
    python -m eval.run_eval --provider ollama --quick

Runs in a temporary database, so your own sessions are not touched. Writes eval/last_report.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ria.config import Settings, get_settings  # noqa: E402
from ria.models.enums import AnalysisMode, EvidenceStatus, FindingType, InputMode  # noqa: E402
from ria.persistence.repositories import Repository  # noqa: E402
from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions  # noqa: E402
from ria.providers.base import LLMProvider  # noqa: E402
from ria.providers.factory import create_provider  # noqa: E402
from ria.services.ingestion import SAMPLE_DIR, IngestionService  # noqa: E402

ACCEPT = {
    "CONTRADICTION": {FindingType.CONTRADICTION},
    "AMBIGUITY": {FindingType.AMBIGUITY, FindingType.UNDEFINED_TERM, FindingType.CONTRADICTION},
    "GAP": {FindingType.GAP},
    "UNDEFINED_TERM": {FindingType.UNDEFINED_TERM},
    "SCOPE_ISSUE": {FindingType.SCOPE_ISSUE},
    "POSSIBLE_TRANSCRIPTION_ERROR": {FindingType.POSSIBLE_TRANSCRIPTION_ERROR},
    "UNKNOWN_SPEAKER": {FindingType.LOW_CONFIDENCE_EVIDENCE},
}


@dataclass
class EvalResult:
    provider: str
    model: str
    found: dict[str, str | None] = field(default_factory=dict)        # planted id -> finding id
    expected: dict[str, dict] = field(default_factory=dict)
    must_not: list[tuple[str, bool, str]] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return sum(1 for v in self.found.values() if v) / max(1, len(self.found))

    def markdown(self) -> str:
        lines = [f"# Evaluation – {self.provider} / {self.model}", "",
                 f"**Planted issues found: {sum(1 for v in self.found.values() if v)} of {len(self.found)} "
                 f"({self.recall:.0%})**", "", "| Planted | Type | Found as | Summary |", "|---|---|---|---|"]
        for pid, fid in self.found.items():
            e = self.expected[pid]
            lines.append(f"| {pid} | {e['type']} | {fid or '❌ missed'} | {e['summary']} |")
        lines += ["", "## Must not happen", "", "| Check | Result | Detail |", "|---|---|---|"]
        for name, ok, detail in self.must_not:
            lines.append(f"| {name} | {'✅ pass' if ok else '❌ FAIL'} | {detail} |")
        lines += ["", "## Metrics", ""] + [f"- {k}: {v}" for k, v in self.metrics.items()]
        return "\n".join(lines) + "\n"


def evaluate(provider: LLMProvider, settings: Settings, quick: bool = False) -> EvalResult:
    with tempfile.TemporaryDirectory() as tmp:
        s = replace(settings, data_dir=Path(tmp))
        repo = Repository(s.db_path)
        ingestion = IngestionService(repo, s)
        sid = repo.create_session("evaluation")
        ingestion.load_samples(sid)
        for d in repo.list_documents(sid):
            ingestion.confirm_extraction(sid, d.document_id)
        run = AnalysisOrchestrator(repo, s, provider).run(
            sid, RunOptions(InputMode.COMPARE, AnalysisMode.QUICK if quick else AnalysisMode.DEEP,
                            redact=provider.sends_data_externally, max_tests_per_story=3))
        return score(repo, sid, run)


def score(repo: Repository, sid: str, run) -> EvalResult:
    expected = json.loads((SAMPLE_DIR / "expected_findings.json").read_text(encoding="utf-8"))
    segments = {s.segment_id: s for s in repo.get_segments(sid)}
    stmts = {s.statement_id: s for s in repo.get_statements(run.run_id)}
    reqs = {r.requirement_id: r for r in repo.get_requirements(run.run_id)}
    findings = repo.get_findings(run.run_id)
    stories = repo.get_stories(run.run_id)
    scenarios = repo.get_scenarios(run.run_id)
    result = EvalResult(provider=run.provider, model=run.model or "-")

    def evidence_text(f) -> str:
        parts = [segments[x].text for x in f.segment_ids if x in segments]
        parts += [stmts[x].evidence_quote for x in f.statement_ids if x in stmts]
        for rid in f.requirement_ids:
            if rid in reqs:
                parts += [segments[x].text for x in reqs[rid].segment_ids if x in segments]
        return " ".join(parts).lower()

    for e in expected["findings"]:
        result.expected[e["id"]] = e
        fragments = [frag.lower() for _, frag in e["sources"]]
        need = 2 if e["type"] == "CONTRADICTION" and len(fragments) >= 2 else 1
        hit = None
        for f in findings:
            if f.finding_type not in ACCEPT[e["type"]]:
                continue
            text = evidence_text(f)
            if sum(1 for frag in set(fragments) if frag in text) >= need:
                hit = f.finding_id
                break
        result.found[e["id"]] = hit

    sms = [r for r in reqs.values() if "sms" in r.description.lower()]
    result.must_not.append(("SMS proposal not treated as decided",
                            all(r.evidence_status != EvidenceStatus.STATED for r in sms)
                            and not any("sms" in s.story_text.lower() for s in stories),
                            ", ".join(f"{r.requirement_id}={r.evidence_status.value}" for r in sms) or "no SMS requirement"))
    stale = [r.requirement_id for r in reqs.values()
             if r.source_statement_ids and all(stmts[x].superseded_by_id for x in r.source_statement_ids if x in stmts)]
    result.must_not.append(("No requirement built only on the corrected 30-day statement", not stale,
                            ", ".join(stale) or "ok"))
    invented = [(a.ac_id, a.unverified_values) for s in stories for a in s.acceptance_criteria if a.unverified_values]
    invented += [(t.scenario_id, t.unverified_values) for t in scenarios if t.unverified_values]
    result.must_not.append(("No values in criteria/tests that are missing from the sources", not invented,
                            str(invented[:5]) if invented else "ok"))
    cur500 = [s.story_id for s in stories for a in s.acceptance_criteria
              if re.search(r"(INR|₹|Rs\.?)\s*500\b", a.text)
              and not any(re.search(r"(INR|₹|Rs\.?)\s*500\b", reqs[r].description) for r in s.requirement_ids if r in reqs)]
    result.must_not.append(("No currency invented for the transcript's '500'", not cur500, ", ".join(cur500) or "ok"))

    total = len(stmts)
    result.metrics = {
        "run status": run.status.value, "statements": total,
        "evidence verified": f"{sum(s.quote_verified for s in stmts.values())}/{total}",
        "unsupported statements": sum(s.evidence_status == EvidenceStatus.UNSUPPORTED for s in stmts.values()),
        "requirements": len(reqs), "findings": len(findings), "stories": len(stories), "test scenarios": len(scenarios),
        "AI calls": run.summary.calls, "tokens": run.summary.input_tokens + run.summary.output_tokens,
        "duration (s)": run.summary.duration_s, "errors": "; ".join(run.summary.errors) or "none",
    }
    return result


def _utf8_console() -> None:
    """Windows consoles (cp1252) cannot print the report's emoji; avoid UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="mock", choices=["mock", "grok", "ollama", "openai_compatible"])
    ap.add_argument("--quick", action="store_true", help="Quick mode (fewer calls, no AI issue review)")
    args = ap.parse_args()
    settings = get_settings()
    result = evaluate(create_provider(args.provider, settings), settings, args.quick)
    report = result.markdown()
    (ROOT / "eval" / "last_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
