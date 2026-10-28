"""Human review: approve / edit / reject / merge / resolve, with an audit trail.

Only this service changes `review_status`. The AI never approves anything.
"""
from __future__ import annotations

from dataclasses import dataclass

from ria.models.analysis import AcceptanceCriterion, RequirementRecord
from ria.models.enums import ItemType, ReviewStatus
from ria.persistence.repositories import Repository
from ria.logging_setup import ctx, get_logger

log = get_logger(__name__)


TABLES = {ItemType.REQUIREMENT: "requirements", ItemType.FINDING: "findings", ItemType.STORY: "stories",
          ItemType.SCENARIO: "scenarios"}
ID_FIELD = {ItemType.REQUIREMENT: "requirement_id", ItemType.FINDING: "finding_id", ItemType.STORY: "story_id",
            ItemType.SCENARIO: "scenario_id"}
EDITABLE = {
    ItemType.REQUIREMENT: {"title", "description", "actors", "trigger", "expected_behavior", "priority",
                           "business_rules", "exceptions"},
    ItemType.FINDING: {"title", "description", "clarification_question", "ask_whom", "severity"},
    ItemType.STORY: {"title", "actor", "capability", "benefit", "priority"},
    ItemType.SCENARIO: {"title", "preconditions", "steps", "expected_result", "priority"},
}

ACTION_STATUS = {
    "approve": ReviewStatus.APPROVED, "reject": ReviewStatus.REJECTED,
    "needs_clarification": ReviewStatus.NEEDS_CLARIFICATION, "out_of_scope": ReviewStatus.OUT_OF_SCOPE,
    "resolve": ReviewStatus.RESOLVED, "reset": ReviewStatus.PENDING,
}


class ReviewError(ValueError):
    pass


@dataclass
class ReviewService:
    repo: Repository

    def _load(self, run_id: str, item_type: ItemType, item_id: str):
        table = TABLES[item_type]
        items = self.repo.get_requirements(run_id) if table == "requirements" else self.repo.get_items(table, run_id)
        item = next((i for i in items if getattr(i, ID_FIELD[item_type]) == item_id), None)
        if item is None:
            raise ReviewError(f"{item_id} not found")
        return item

    def apply(self, *, session_id: str, run_id: str, item_type: ItemType, item_id: str, action: str,
              reviewer: str | None = None, note: str | None = None, edits: dict | None = None):
        """Apply one review action. Returns the updated item."""
        item_type = ItemType(item_type)
        if action not in ACTION_STATUS and action != "edit":
            raise ReviewError(f"Unknown action '{action}'")
        if action == "resolve" and item_type != ItemType.FINDING:
            raise ReviewError("Only findings can be resolved")
        item = self._load(run_id, item_type, item_id)
        changed = False
        for field, value in (edits or {}).items():
            if field not in EDITABLE[item_type]:
                raise ReviewError(f"Field '{field}' cannot be edited on a {item_type.value}")
            old = getattr(item, field)
            if old == value or (old in (None, "", []) and value in (None, "", [])):
                continue
            setattr(item, field, value)
            changed = True
            self.repo.add_audit(run_id=run_id, session_id=session_id, item_type=item_type.value, item_id=item_id,
                                action="edit", field=field, old=_s(old), new=_s(value), reviewer=reviewer, note=note)
        if action == "edit":
            if not changed:
                return item
            new_status = ReviewStatus.EDITED
        else:
            new_status = ACTION_STATUS[action]
            if action == "approve" and (changed or item.review_status == ReviewStatus.EDITED):
                new_status = ReviewStatus.EDITED          # approved with changes
        if action == "resolve":
            item.resolution = note
        old_status = item.review_status
        item.review_status = new_status
        if note:
            item.reviewer_note = note
        self.repo.save_item(TABLES[item_type], item)
        self.repo.add_audit(run_id=run_id, session_id=session_id, item_type=item_type.value, item_id=item_id,
                            action=action, field="review_status", old=old_status.value, new=new_status.value,
                            reviewer=reviewer, note=note)
        log.info("Review: %s %s %s -> %s (%s) by %s", item_type.value, item_id, old_status.value, new_status.value,
                 action, "named reviewer" if reviewer else "anonymous", extra=ctx(session_id, run_id))
        return item

    def bulk(self, *, session_id: str, run_id: str, item_type: ItemType, item_ids: list[str], action: str,
             reviewer: str | None = None, note: str | None = None) -> int:
        for i in item_ids:
            self.apply(session_id=session_id, run_id=run_id, item_type=item_type, item_id=i, action=action,
                       reviewer=reviewer, note=note)
        return len(item_ids)

    def merge_requirements(self, *, session_id: str, run_id: str, keep_id: str, merge_ids: list[str],
                           reviewer: str | None = None) -> RequirementRecord:
        """Keep one requirement, fold the evidence of the others into it, reject the others."""
        reqs = {r.requirement_id: r for r in self.repo.get_requirements(run_id)}
        if keep_id not in reqs or any(m not in reqs for m in merge_ids):
            raise ReviewError("Unknown requirement ID in merge")
        keep = reqs[keep_id]
        for mid in merge_ids:
            if mid == keep_id:
                continue
            other = reqs[mid]
            for attr in ("source_statement_ids", "segment_ids", "document_ids", "unresolved_questions"):
                merged = list(dict.fromkeys(getattr(keep, attr) + getattr(other, attr)))
                setattr(keep, attr, merged)
            other.review_status = ReviewStatus.REJECTED
            other.reviewer_note = f"Merged into {keep_id}"
            self.repo.save_requirements([other])
            self.repo.add_audit(run_id=run_id, session_id=session_id, item_type="requirement", item_id=mid,
                                action="merge", field="review_status", new="REJECTED", reviewer=reviewer,
                                note=f"Merged into {keep_id}")
        keep.review_status = ReviewStatus.EDITED
        self.repo.save_requirements([keep])
        self.repo.add_audit(run_id=run_id, session_id=session_id, item_type="requirement", item_id=keep_id,
                            action="merge", field="source_statement_ids", new=", ".join(keep.source_statement_ids),
                            reviewer=reviewer, note=f"Merged {', '.join(m for m in merge_ids if m != keep_id)}")
        return keep

    def edit_criterion(self, *, session_id: str, run_id: str, story_id: str, ac_id: str, given: str, when: str,
                       then: str, reviewer: str | None = None):
        story = self._load(run_id, ItemType.STORY, story_id)
        ac: AcceptanceCriterion | None = next((a for a in story.acceptance_criteria if a.ac_id == ac_id), None)
        if ac is None:
            raise ReviewError(f"{ac_id} not found")
        old = ac.text
        ac.given, ac.when, ac.then = given, when, then
        ac.unverified_values = []   # a human has checked it
        story.review_status = ReviewStatus.EDITED
        self.repo.save_item("stories", story)
        self.repo.add_audit(run_id=run_id, session_id=session_id, item_type="story", item_id=story_id,
                            action="edit", field=ac_id, old=old, new=ac.text, reviewer=reviewer)
        return story


def _s(v) -> str:
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return "" if v is None else str(getattr(v, "value", v))
