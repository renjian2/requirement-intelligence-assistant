"""Milestone 2 · Review & Analysis – one screen: requirements with business statuses, the issues linked to
each requirement (resolve them in place), and a clarification email / questions CSV for the current filter."""
from __future__ import annotations

import csv
import io
from collections import defaultdict

import streamlit as st

from ria.logging_setup import ctx, get_logger, setup_logging

setup_logging()
log = get_logger("ui.review")

from ria.models.enums import ACCEPTED_REVIEW, FINDING_LABELS, EvidenceStatus, ItemType, Priority, ReviewStatus  # noqa: E402
from ria.services.resolution import (  # noqa: E402
    STATUS_ORDER, Bucket, ReqStatus, finding_bucket, findings_in_scope, requirement_statuses, stakeholder_findings,
)
from ria.services.review import ReviewError, ReviewService  # noqa: E402
from ria.services.traceability import clarification_questions  # noqa: E402
from ria.ui.common import get_services, require_session, reviewer_input, run_picker, setup_page, show_segment  # noqa: E402
from ria.ui.integrations import render_outlook_panel  # noqa: E402

setup_page("Review & Analysis", "🔎")
repo, _ = get_services()
sid = require_session()
reviewer = reviewer_input()
st.title("🔎 Review & Analysis")
run = run_picker(repo, sid, key="review_run")
if run is None:
    st.info("No analysis yet. Upload documents and press **⚡ Run Complete AI Analysis** first.")
    st.page_link("pages/1_Prepare_and_Run.py", label="Go to Prepare & Run", icon="🚀")
    st.stop()
session = repo.get_session(sid) or {"name": "Untitled"}
st.caption(f"📅 {run.created_at:%d %b %Y} · Session: **{session['name']}**")
if run.is_demo:
    st.caption("🧪 Demo data – produced by the Demo provider, not by AI.")

review = ReviewService(repo)
reqs = repo.get_requirements(run.run_id)
findings = repo.get_findings(run.run_id)
stmts = {s.statement_id: s for s in repo.get_statements(run.run_id)}
docs = {d.document_id: d for d in repo.list_documents(sid)}
segments = {s.segment_id: s for s in repo.get_segments(sid)}
status = requirement_statuses(reqs, findings)
by_id = {r.requirement_id: r for r in reqs}
OPEN = (ReviewStatus.RESOLVED, ReviewStatus.REJECTED)


def doc_names(ids) -> str:
    return ", ".join(docs[d].file_name if d in docs else d for d in ids)


def act(item_type, item_id, action, note=None, edits=None) -> None:
    try:
        review.apply(session_id=sid, run_id=run.run_id, item_type=item_type, item_id=item_id, action=action,
                     reviewer=reviewer, note=note, edits=edits)
    except ReviewError as exc:
        st.error(str(exc))
        return
    st.rerun()


def is_clean(r) -> bool:
    return (status[r.requirement_id] == ReqStatus.REVIEW and r.evidence_status == EvidenceStatus.STATED
            and r.confidence >= 75 and not r.is_ai_suggestion)


# ---------------------------------------------------------------- status overview
counts = defaultdict(int)
for v in status.values():
    counts[v] += 1
display_statuses = STATUS_ORDER
for col, s in zip(st.columns(len(display_statuses)), display_statuses):
    col.metric(s.value, counts[s])
st.progress(counts[ReqStatus.APPROVED] / len(reqs) if reqs else 0.0,
            text=f"{counts[ReqStatus.APPROVED]} of {len(reqs)} requirements approved – approved requirements flow "
                 "to Stories & Tests")
if not reqs:
    st.info("This run produced no requirements.")
    st.stop()

# ---------------------------------------------------------------- filters
f1, f2, f3 = st.columns([2, 1.5, 2.5])
s_sel = f1.multiselect("Status", [s.value for s in STATUS_ORDER])
p_sel = f2.multiselect("Priority", [p.value for p in Priority] + ["Not set"])
q = f3.text_input("Search", placeholder="ID, title or text")
filtered = bool(s_sel or p_sel or q)
view = [r for r in reqs
        if (not s_sel or status[r.requirement_id].value in s_sel)
        and (not p_sel or (r.priority.value if r.priority else "Not set") in p_sel)
        and (not q or q.lower() in f"{r.requirement_id} {r.title} {r.description}".lower())]
scope_ids = {r.requirement_id for r in view} if filtered else None
open_by_req = defaultdict(int)
for f in findings:
    if f.review_status not in OPEN:
        for rid in f.requirement_ids:
            open_by_req[rid] += 1

# ---------------------------------------------------------------- toolbar
stake = [f for f in findings_in_scope(stakeholder_findings(findings), scope_ids) if f.review_status not in OPEN]
questions = clarification_questions(stake, [r for r in reqs if scope_ids is None or r.requirement_id in scope_ids],
                                    stmts)
clean = [r for r in view if is_clean(r)]
t1, t2, t3 = st.columns([1.6, 1.4, 2])
mail_key = f"_mail_open_{sid}"
open_issues_count = sum(1 for r in view if status.get(r.requirement_id) == ReqStatus.CONFLICT) or len(stake)
if t1.button(f"📧 Draft clarification email ({open_issues_count} issue(s))", type="primary", use_container_width=True):
    st.session_state[mail_key] = not st.session_state.get(mail_key, False)
buf = io.StringIO()
w = csv.writer(buf)
w.writerow(["Ask whom", "Question", "Severity", "Source", "Requirements", "Answer"])
for item in questions:
    w.writerow([item.ask_whom, item.question, item.severity, item.source, ", ".join(item.requirement_ids), ""])
t2.download_button(f"Download questions (CSV) · {len(questions)}", buf.getvalue().encode("utf-8-sig"),
                   file_name=f"stakeholder_questions_{run.run_id}.csv", mime="text/csv", use_container_width=True,
                   on_click=lambda: log.info("Questions CSV downloaded (%d)", len(questions), extra=ctx(sid, run.run_id)))
if t3.button(f"⚡ Approve {len(clean)} clean requirement(s) shown", disabled=not clean, use_container_width=True,
             help="Only ⚪ Confirm Requirement requirements that are stated in the sources, verified and high-confidence."):
    review.bulk(session_id=sid, run_id=run.run_id, item_type=ItemType.REQUIREMENT,
                item_ids=[r.requirement_id for r in clean], action="approve", reviewer=reviewer,
                note="Bulk approval: clean, verified requirement")
    st.rerun()
if st.session_state.get(mail_key):
    with st.container(border=True):
        render_outlook_panel(repo, sid, run, key="review_mail", requirement_ids=scope_ids,
                             scope_label=("Covers the requirements currently shown by your filters."
                                          if filtered else "Covers all open conflicts and business questions. "
                                          "Use the filters above to send a narrower email.")
                             + " Internal wording notes are never included.")

# ---------------------------------------------------------------- requirements table
st.caption(f"Showing {len(view)} of {len(reqs)} requirements")
st.dataframe([{"ID": r.requirement_id, "Status": status[r.requirement_id].value, "Title": r.title,
               "Functional area": r.group, "Priority": r.priority.value if r.priority else "Not set",
               "Open issues": open_by_req[r.requirement_id], "Sources": doc_names(r.document_ids)}
              for r in view], hide_index=True, use_container_width=True,
             column_config={"Title": st.column_config.TextColumn(width="large")})

if not view:
    st.stop()

# ---------------------------------------------------------------- deep-dive
st.subheader("Requirement deep-dive")
# Stable key: the label shows the status, which changes after Approve – without a key Streamlit would treat
# it as a new widget and jump back to the first requirement.
if st.session_state.get("deep_req") not in [r.requirement_id for r in view]:
    st.session_state["deep_req"] = view[0].requirement_id
rid = st.selectbox("Requirement", [r.requirement_id for r in view], key="deep_req",
                   format_func=lambda i: f"{i} · {status[i].value} · {by_id[i].title}")
r = by_id[rid]
with st.container(border=True):
    st.markdown(f"### {r.requirement_id} · {r.title}")
    st.markdown(f"**{status[rid].value}** · area **{r.group}** · "
                f"priority **{r.priority.value if r.priority else 'not set'}**"
                + (" · 🤖 AI suggestion" if r.is_ai_suggestion else ""))
    st.write(r.description)
    for k, v in {"Actors": ", ".join(r.actors), "Trigger": r.trigger, "Expected behaviour": r.expected_behavior,
                 "Business rules": "; ".join(r.business_rules), "Exceptions": "; ".join(r.exceptions)}.items():
        if v:
            st.markdown(f"**{k}:** {v}")
    if r.reviewer_note:
        st.caption(f"Reviewer note: {r.reviewer_note}")

    # Issues linked to this requirement – resolve them here.
    linked = [f for f in findings if rid in f.requirement_ids and f.review_status not in OPEN]
    for f in linked:
        internal = finding_bucket(f) == Bucket.QUALITY
        icon = "🔴" if f.finding_type.value == "CONTRADICTION" else ("✏️" if internal else "🟡")
        with st.container(border=True):
            st.markdown(f"{icon} **{f.finding_id} · {f.title}** · {FINDING_LABELS[f.finding_type]} · "
                        f"{f.severity.value}" + (" · *internal wording note*" if internal else ""))
            if f.values:
                st.dataframe([{"Requirement ID": v.requirement_id, "Stated value": v.value,
                               "Source document": doc_names(v.document_ids)} for v in f.values],
                             hide_index=True, use_container_width=True)
            elif f.description:
                st.caption(f.description)
            if f.clarification_question:
                st.markdown(f"❓ **Ask {f.ask_whom or 'the business owner'}:** {f.clarification_question}")
            c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="bottom")
            note = c1.text_input("Answer / resolution", key=f"note_{f.finding_id}",
                                 placeholder="e.g. Finance confirmed 45 days")
            if c2.button("✔️ Resolve", key=f"res_{f.finding_id}", use_container_width=True):
                if not note:
                    st.warning("Enter the answer or resolution first.")
                else:
                    act(ItemType.FINDING, f.finding_id, "resolve", note)
            if c3.button("❌ Not an issue", key=f"dis_{f.finding_id}", use_container_width=True):
                act(ItemType.FINDING, f.finding_id, "reject", note or None)
    if r.vague_terms and not any(finding_bucket(f) == Bucket.QUALITY for f in linked):
        st.warning("Not testable as written: " + ", ".join(f"“{t}”" for t in r.vague_terms))

    a1, a2, a3, a4 = st.columns(4)
    if a1.button("✅ Approve", key=f"appr_{rid}", type="primary", use_container_width=True,
                 disabled=r.review_status in ACCEPTED_REVIEW):
        act(ItemType.REQUIREMENT, rid, "approve")
    with a2.popover("✏️ Request Changes / Edit", use_container_width=True):
        with st.form(f"edit_{rid}"):
            title = st.text_input("Title", r.title)
            desc = st.text_area("Description", r.description)
            actors = st.text_input("Actors (comma-separated)", ", ".join(r.actors))
            note = st.text_input("Note / requested change")
            b1, b2 = st.columns(2)
            save_approve = b1.form_submit_button("Save & approve", type="primary")
            save_request = b2.form_submit_button("Request changes")
        edits = {"title": title, "description": desc, "actors": [a.strip() for a in actors.split(",") if a.strip()]}
        if save_approve:
            act(ItemType.REQUIREMENT, rid, "approve", note or None, edits)
        if save_request:
            act(ItemType.REQUIREMENT, rid, "needs_clarification", note or "Changes requested", edits)
    if a3.button("❌ Reject", key=f"rej_{rid}", use_container_width=True):
        act(ItemType.REQUIREMENT, rid, "reject")
    if a4.button("↩️ Reset", key=f"reset_{rid}", use_container_width=True,
                 disabled=r.review_status == ReviewStatus.PENDING):
        act(ItemType.REQUIREMENT, rid, "reset")

    area_clean = [x for x in reqs if x.group == r.group and is_clean(x)]
    if area_clean and st.button(f"✅ Approve all {len(area_clean)} clean requirement(s) in '{r.group}'",
                                key=f"area_{r.group}"):
        review.bulk(session_id=sid, run_id=run.run_id, item_type=ItemType.REQUIREMENT,
                    item_ids=[x.requirement_id for x in area_clean], action="approve", reviewer=reviewer,
                    note=f"Area approval: {r.group}")
        st.rerun()

    with st.expander(f"Evidence – {len(r.source_statement_ids)} source statement(s)"):
        for st_id in r.source_statement_ids:
            s = stmts.get(st_id)
            if not s:
                continue
            st.markdown(f"**{s.statement_id}** · {s.statement_type.value.replace('_', ' ').title()}"
                        f" · {'✅ quote verified' if s.quote_verified else '⚠️ quote not found'}"
                        + (f" · corrected by {s.superseded_by_id}" if s.superseded_by_id else ""))
            st.markdown(f"> *“{s.evidence_quote}”*")
            for seg_id in s.segment_ids:
                if seg_id in segments:
                    show_segment(segments[seg_id], docs[segments[seg_id].document_id].file_name
                                 if segments[seg_id].document_id in docs else "")
