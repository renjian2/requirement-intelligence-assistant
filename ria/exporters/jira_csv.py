"""Jira-importable CSV of user stories (File → Import issues from CSV)."""
from __future__ import annotations

import csv
import io

from ria.exporters.bundle import ExportBundle

PRIORITY = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}


def build_jira_csv(b: ExportBundle) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Summary", "Issue Type", "Priority", "Description", "Acceptance Criteria", "Labels", "External ID"])
    reqs = {r.requirement_id: r for r in b.requirements}
    for s in b.included(b.stories):
        tests = [t for t in b.included(b.scenarios) if t.story_id == s.story_id]
        desc = [s.story_text, "", f"Requirements: {', '.join(s.requirement_ids)}"]
        for rid in s.requirement_ids:
            if rid in reqs:
                desc.append(f"- {rid}: {reqs[rid].description}")
        if tests:
            desc += ["", "Important test scenarios:"] + [f"- {t.scenario_id} [{t.priority.value}] {t.title}" for t in tests]
        if s.clarification_questions:
            desc += ["", "Open questions:"] + [f"- {q}" for q in s.clarification_questions]
        ac = "\n".join(f"{a.ac_id}: Given {a.given}\nWhen {a.when}\nThen {a.then}" for a in s.acceptance_criteria)
        labels = " ".join(x for x in ["RIA", s.group.replace(" ", "_")[:40],
                                      "needs-review" if s.review_status.value == "PENDING" else ""] if x)
        w.writerow([s.title[:250], "Story", PRIORITY.get(s.priority.value if s.priority else "", "Medium"),
                    "\n".join(desc), ac, labels, s.story_id])
    return buf.getvalue().encode("utf-8-sig")   # BOM so Excel/Jira read UTF-8 correctly
