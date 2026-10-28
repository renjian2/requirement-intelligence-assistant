"""Home: Session Dashboard & History."""
from __future__ import annotations

import streamlit as st

from ria.logging_setup import setup_logging

setup_logging()

from ria.models.enums import ReviewStatus
from ria.persistence.repositories import SessionNameError
from ria.ui.common import get_services, set_active_session

st.set_page_config(page_title="RIA · Session Dashboard", page_icon="🏠", layout="wide")
repo, service = get_services()


# ---------------------------------------------------------------- callbacks (run before the page redraws)
def _ask_delete(session_id: str) -> None:
    st.session_state["_confirm_delete"] = session_id


def _cancel_delete() -> None:
    st.session_state.pop("_confirm_delete", None)


def _do_delete(session_id: str, name: str) -> None:
    repo.delete_session(session_id)
    if st.session_state.get("session_id") == session_id:
        set_active_session(None)
    st.session_state.pop("_confirm_delete", None)
    st.session_state["_home_flash"] = f"Session '{name}' deleted."


def _open_session(session_id: str) -> None:
    set_active_session(session_id)


st.title("🏠 Requirement Intelligence Assistant")
st.caption("Turn workshop transcripts and specifications into verified requirements, resolved conflicts, "
           "Jira-ready stories and Outlook clarification emails.")
if msg := st.session_state.pop("_home_flash", None):
    st.success(msg)

# ---------------------------------------------------------------- create sessions
c_new, c_demo, _ = st.columns([1.3, 1.6, 4])
with c_new.popover("➕ New session", use_container_width=True):
    with st.form("new_session_form", border=False):
        name = st.text_input("Session name", max_chars=100, placeholder="Example: Member Portal requirements")
        create = st.form_submit_button("Create session", type="primary", use_container_width=True)
    if create:
        try:
            sid = repo.create_session(name)
        except SessionNameError as exc:
            st.error(f"⚠️ {exc}")
        else:
            set_active_session(sid)
            st.switch_page("pages/1_Prepare_and_Run.py")
if c_demo.button("🧪 Load demo session (synthetic data)", use_container_width=True):
    sid = repo.create_session(repo.unique_session_name("Northwind expense portal (demo)"))
    service.load_samples(sid)
    set_active_session(sid)
    st.switch_page("pages/1_Prepare_and_Run.py")

# ---------------------------------------------------------------- data
rows = []
for s in repo.list_sessions():
    runs = repo.list_runs(s["session_id"])
    latest = runs[0] if runs else None
    reqs = repo.get_requirements(latest.run_id) if latest else []
    findings = repo.get_findings(latest.run_id) if latest else []
    rows.append({**s, "latest": latest, "requirements": len(reqs),
                 "approved": sum(r.review_status in (ReviewStatus.APPROVED, ReviewStatus.EDITED) for r in reqs),
                 "findings": sum(f.review_status not in (ReviewStatus.RESOLVED, ReviewStatus.REJECTED)
                                 for f in findings),
                 "stories": latest.summary.stories if latest else 0})
active_id = st.session_state.get("session_id") or st.query_params.get("s")
active = next((r for r in rows if r["session_id"] == active_id), None)

# ---------------------------------------------------------------- active session + milestones
st.subheader("Active session")
if active:
    latest = active["latest"]
    m = st.columns(4)
    m[0].metric("Documents", active["doc_count"])
    m[1].metric("Requirements", active["requirements"], f"{active['approved']} approved", delta_color="off",
                delta_arrow="off")
    m[2].metric("Open issues", active["findings"])
    m[3].metric("User stories", active["stories"])
    st.caption(f"**{active['name']}**")
else:
    st.info("Open a session below, create a new one, or load the demo.")

st.subheader("Workflow")
workflow = [
    ("1", "Prepare & Run", "Upload documents · one-click AI analysis", "pages/1_Prepare_and_Run.py", "🚀"),
    ("2", "Review & Analysis", "Approve requirements · resolve conflicts · Outlook", "pages/2_Review_and_Analysis.py", "🔎"),
    ("3", "Stories & Tests", "Agile story board · Gherkin · QA · Jira", "pages/3_Stories_and_Tests.py", "📝"),
    ("4", "Export", "Traceability · Word BRD · Excel · Jira CSV", "pages/4_Export.py", "📦"),
]
for col, (n, title, text, page, icon) in zip(st.columns(4), workflow):
    with col.container(border=True):
        st.markdown(f"#### {icon} {n}. {title}")
        st.caption(text)
        st.page_link(page, label=f"Open {title}", icon=icon, use_container_width=True)

# ---------------------------------------------------------------- session history
st.subheader("Session history")
search = st.text_input("Search sessions", placeholder="Search by session name")
if search.strip():
    rows = [r for r in rows if search.casefold().strip() in r["name"].casefold()]

W = [2.5, 0.8, 1.1, 0.8, 1.4, 1.6, 1.2]
for col, text in zip(st.columns(W), ["Session name", "Docs", "Reqs", "Issues",
                                     "Latest run", "", ""]):
    col.markdown(f"**{text}**")
for r in rows:
    sid, latest = r["session_id"], r["latest"]
    cols = st.columns(W, vertical_alignment="center")
    # markdown (not st.write) so numbers render as plain text, not code badges
    cols[0].markdown(("**▶ " + r["name"] + "**") if sid == active_id else r["name"])
    cols[1].markdown(str(r["doc_count"]))
    cols[2].markdown(f"{r['requirements']} ({r['approved']} ✓)" if r["requirements"] else "0")
    cols[3].markdown(str(r["findings"]))
    cols[4].markdown(latest.status.value.replace("_", " ").title() if latest else "Not analysed")
    cols[5].button("📂 Open Session", key=f"open_{sid}", use_container_width=True, disabled=sid == active_id,
                   on_click=_open_session, args=(sid,))
    cols[6].button("🗑️ Delete", key=f"delete_{sid}", use_container_width=True, on_click=_ask_delete, args=(sid,))
    if st.session_state.get("_confirm_delete") == sid:
        with st.container(border=True):
            st.warning(f"Delete **{r['name']}** permanently? This removes its {r['doc_count']} document(s), "
                       "analysis runs, reviews and logs. This cannot be undone.")
            yes, no, _ = st.columns([1.4, 1, 3])
            yes.button("Yes, delete permanently", key=f"yes_delete_{sid}", type="primary", use_container_width=True,
                       on_click=_do_delete, args=(sid, r["name"]))
            no.button("Cancel", key=f"cancel_delete_{sid}", use_container_width=True, on_click=_cancel_delete)
if not rows:
    st.caption("No matching sessions." if search.strip() else "No sessions yet.")
