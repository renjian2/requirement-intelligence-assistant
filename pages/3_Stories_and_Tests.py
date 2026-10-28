"""Milestone 3 · Stories & Tests – Jira-style story board connected to requirement approval.
The requirement text appears once per card (in the narrative); criteria and tests are collapsed."""
from __future__ import annotations

import streamlit as st

from ria.logging_setup import ctx, get_logger, setup_logging

setup_logging()
log = get_logger("ui.stories")

from ria.config import get_settings  # noqa: E402
from ria.models.enums import ACCEPTED_REVIEW, EXCLUDED_REVIEW, ItemType, ProviderName, ReviewStatus  # noqa: E402
from ria.pipeline.orchestrator import AnalysisOrchestrator  # noqa: E402
from ria.providers.base import ProviderError  # noqa: E402
from ria.providers.factory import create_provider, describe_providers  # noqa: E402
from ria.services.resolution import ReqStatus, requirement_statuses  # noqa: E402
from ria.services.review import ReviewError, ReviewService  # noqa: E402
from ria.ui.common import get_services, require_session, reviewer_input, run_picker, setup_page  # noqa: E402
from ria.ui.integrations import render_jira_workbench  # noqa: E402
from ria.ui.review import status_badge  # noqa: E402

setup_page("Stories & Tests", "📝")
repo, _ = get_services()
sid = require_session()
reviewer = reviewer_input()
st.title("📝 Stories & Tests")
run = run_picker(repo, sid, key="stories_run")
if run is None:
    st.info("No analysis yet. Run it on **Prepare & Run** first.")
    st.page_link("pages/1_Prepare_and_Run.py", label="Go to Prepare & Run", icon="🚀")
    st.stop()
session = repo.get_session(sid) or {"name": "Untitled"}
st.caption(f"📅 {run.created_at:%d %b %Y} · Session: **{session['name']}**")

review = ReviewService(repo)
reqs = {r.requirement_id: r for r in repo.get_requirements(run.run_id)}
status = requirement_statuses(list(reqs.values()), repo.get_findings(run.run_id))
stories = repo.get_stories(run.run_id)
scenarios = repo.get_scenarios(run.run_id)
approved = [r for r in reqs.values() if r.review_status in ACCEPTED_REVIEW]

st.markdown(f"**📊 Requirement Alignment: {len(approved)} of {len(reqs)} requirements approved in Review & Analysis**")
st.progress(len(approved) / len(reqs) if reqs else 0.0)


def sprint_ready(s) -> bool:
    return bool(s.requirement_ids) and all(reqs.get(r) and reqs[r].review_status in ACCEPTED_REVIEW
                                           for r in s.requirement_ids)


def act(item_type, item_id, action, note=None) -> None:
    try:
        review.apply(session_id=sid, run_id=run.run_id, item_type=item_type, item_id=item_id, action=action,
                     reviewer=reviewer, note=note)
    except ReviewError as exc:
        st.error(str(exc))
        return
    st.rerun()


REQ_ICON = {ReqStatus.APPROVED: "🟢", ReqStatus.CONFLICT: "🔴", ReqStatus.CLARIFY: "🟡", ReqStatus.REVIEW: "⚪",
            ReqStatus.EXCLUDED: "❌"}


# ---------------------------------------------------------------- regenerate (requirements changed?)
# Review changes made after the stories were generated (audit ids are strictly increasing).
changed = [a for a in repo.get_audit(run.run_id) if a["id"] > run.summary.stories_audit_mark
           and a["item_type"] == "requirement" and a["field"] in ("review_status", "description", "title", "actors")]
settings = get_settings()
infos = {i.name: i for i in describe_providers(settings)}
provider_name = st.session_state.get("adv_provider") or next(
    (n for n, i in infos.items() if i.configured and n != ProviderName.MOCK), ProviderName.MOCK)
with st.container(border=bool(changed)):
    r1, r2 = st.columns([4, 1.4], vertical_alignment="center")
    if changed:
        r1.warning(f"Requirements changed after these stories were generated ({len(changed)} review change(s)). "
                   "Regenerate so stories use the approved / edited requirements and skip rejected ones.", icon="🔁")
    else:
        r1.caption("Stories are generated from the current requirements. Regenerate after review changes.")
    if r2.button("🔁 Regenerate stories & tests", type="primary" if changed else "secondary",
                 use_container_width=True, key="regen"):
        bar = st.progress(0.0, text="Starting…")
        try:
            cont = AnalysisOrchestrator(repo, settings, create_provider(provider_name, settings)).continue_run(
                run.run_id, find_issues=False, generate_stories=True,
                max_tests_per_story=st.session_state.get("adv_tests", run.max_tests_per_story),
                progress=lambda msg, frac: bar.progress(min(max(frac, 0.0), 1.0), text=msg))
        except (ProviderError, ValueError) as exc:
            st.error(str(exc))
        else:
            log.info("Stories regenerated: %d stories, %d tests", cont.summary.stories, cont.summary.scenarios,
                     extra=ctx(sid, run.run_id))
            st.session_state["_regen_msg"] = (f"{cont.summary.stories} stories and {cont.summary.scenarios} tests "
                                              "regenerated.")
            st.rerun()
if msg := st.session_state.pop("_regen_msg", None):
    st.success(msg)
stories = repo.get_stories(run.run_id)
scenarios = repo.get_scenarios(run.run_id)
PRIORITY_BADGE = {"HIGH": "🔺 High", "MEDIUM": "🔸 Medium", "LOW": "🔹 Low"}

tab_board, tab_jira = st.tabs(["📋 Story board", "🧩 Jira push"])
with tab_board:
    if not stories:
        st.info("This run has no user stories. Use **🔁 Regenerate stories & tests** above.")
    else:
        c1, c2 = st.columns([2, 2])
        scope = c1.radio("Scope", ["🟢 Sprint-Ready Only (Stories linked to approved requirements)", "All Stories"],
                         key="story_scope")
        story_statuses = sorted({s.review_status.value for s in stories})
        selected_statuses = c2.multiselect("Status", story_statuses, key="story_status_filter")
        ready_only = scope.startswith("🟢")
        view = [s for s in stories if (not ready_only or sprint_ready(s)) and (not selected_statuses or s.review_status.value in selected_statuses)]
        st.caption(f"Showing {len(view)} of {len(stories)} stories · {sum(sprint_ready(s) for s in stories)} sprint-ready")
        if ready_only and not view:
            st.info("No sprint-ready stories yet. Approve requirements in **Review & Analysis**, or switch to "
                    "**All Stories**.")
        stale = [s.story_id for s in stories if any(reqs.get(r) and reqs[r].review_status in EXCLUDED_REVIEW
                                                    for r in s.requirement_ids)]
        if stale:
            st.warning(f"{', '.join(stale)} use rejected requirements – use 🔁 Regenerate above.")

        for s in view:
            with st.container(border=True):
                h1, h2 = st.columns([5, 2])
                h1.markdown(f"#### {s.story_id} · {s.title}")
                h2.markdown(f"{PRIORITY_BADGE.get(s.priority.value if s.priority else '', '▫️ No priority')} · "
                            f"{status_badge(s.review_status)}")
                st.caption("Requirements: " + "  ".join(
                    f"{REQ_ICON.get(status.get(r), '▫️')} {r}" for r in s.requirement_ids))
                st.markdown(f"**Description:**  \n👤 As a **{s.actor}**, I want to **{s.capability}**, so that **{s.benefit}**.")
                st.markdown(f"**Path or Navigation:** `{s.group} → {s.title}`")
                req_lines = []
                for r in s.requirement_ids:
                    req = reqs.get(r)
                    icon = REQ_ICON.get(status.get(r), "▫️")
                    if req:
                        desc = f": {req.description}" if (req.description and req.description != req.title) else ""
                        req_lines.append(f"{icon} **{r}** – **{req.title}**{desc}")
                    else:
                        req_lines.append(f"{icon} **{r}**")
                if req_lines:
                    st.markdown("**Functional Requirement:**  \n" + "  \n".join(f"- {line}" for line in req_lines))
                else:
                    st.markdown("**Functional Requirement:**  \n*No linked requirements*")
                with st.expander(f"Acceptance Requirement ({len(s.acceptance_criteria)}) & QA tests "
                                 f"({sum(t.story_id == s.story_id for t in scenarios)})"):
                    for ac in s.acceptance_criteria:
                        with st.container(border=True):
                            a1, a2 = st.columns([6, 1])
                            a1.markdown(f"**{ac.ac_id}** · {ac.criterion_type.value.replace('_', ' ').title()}  \n"
                                        f"**Given** {ac.given}  \n**When** {ac.when}  \n**Then** {ac.then}")
                            if ac.unverified_values:
                                a1.error("Values not found in the sources: " + ", ".join(ac.unverified_values))
                            with a2.popover("✏️ Quick Edit"):
                                with st.form(f"ac_{ac.ac_id}"):
                                    g = st.text_area("Given", ac.given)
                                    w = st.text_area("When", ac.when)
                                    t = st.text_area("Then", ac.then)
                                    if st.form_submit_button("Save", type="primary"):
                                        review.edit_criterion(session_id=sid, run_id=run.run_id, story_id=s.story_id,
                                                              ac_id=ac.ac_id, given=g, when=w, then=t,
                                                              reviewer=reviewer)
                                        st.rerun()
                    tests = [t for t in scenarios if t.story_id == s.story_id]
                    for label, group in (("✅ Happy path", [t for t in tests if t.scenario_type.value == "POSITIVE"]),
                                         ("⚠️ Edge cases / error handling",
                                          [t for t in tests if t.scenario_type.value != "POSITIVE"])):
                        if not group:
                            continue
                        st.markdown(f"**{label}**")
                        for t in group:
                            steps = "  \n".join(f"{i}. {x}" for i, x in enumerate(t.steps, start=1))
                            st.markdown(f"**{t.scenario_id}** [{t.priority.value}] {t.title}  \n{steps}  \n"
                                        f"**Expected:** {t.expected_result}")
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("✅ Approve", key=f"s_appr_{s.story_id}", use_container_width=True,
                             disabled=s.review_status in ACCEPTED_REVIEW):
                    act(ItemType.STORY, s.story_id, "approve")
                if b2.button("❓ Needs clarification", key=f"s_clar_{s.story_id}", use_container_width=True):
                    act(ItemType.STORY, s.story_id, "needs_clarification")
                if b3.button("❌ Reject", key=f"s_rej_{s.story_id}", use_container_width=True):
                    act(ItemType.STORY, s.story_id, "reject")
                if b4.button("↩️ Reset", key=f"s_reset_{s.story_id}", use_container_width=True,
                             disabled=s.review_status == ReviewStatus.PENDING):
                    act(ItemType.STORY, s.story_id, "reset")

with tab_jira:
    render_jira_workbench(repo, sid, run, key="stories_jira")
