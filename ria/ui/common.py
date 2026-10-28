"""Shared Streamlit helpers: cached services, session picker, warning display."""
from __future__ import annotations

import streamlit as st

from ria.config import get_settings
from ria.models.enums import WarningSeverity
from ria.models.segments import ExtractionWarning
from ria.persistence.repositories import Repository
from ria.services.ingestion import IngestionService

_SEVERITY_ICON = {WarningSeverity.BLOCKING: "⛔", WarningSeverity.WARNING: "⚠️", WarningSeverity.INFO: "ℹ️"}


@st.cache_resource
def get_services() -> tuple[Repository, IngestionService]:
    settings = get_settings()
    repo = Repository(settings.db_path)
    return repo, IngestionService(repo, settings)


def setup_page(title: str, icon: str = "📋") -> None:
    st.set_page_config(page_title=f"{title} · Requirement Intelligence", page_icon=icon, layout="wide")


def active_session_id() -> str | None:
    """Active session is kept in the URL (?s=...) so a browser refresh keeps it."""
    sid = st.session_state.get("session_id") or st.query_params.get("s")
    repo, _ = get_services()
    if sid and repo.get_session(sid):
        st.session_state["session_id"] = sid
        return sid
    st.session_state.pop("session_id", None)
    return None


def set_active_session(session_id: str | None) -> None:
    if session_id:
        st.session_state["session_id"] = session_id
        st.query_params["s"] = session_id
    else:
        st.session_state.pop("session_id", None)
        st.query_params.clear()


def session_sidebar() -> str | None:
    repo, _ = get_services()
    sessions = repo.list_sessions()
    current = active_session_id()
    with st.sidebar:
        st.markdown("### Analysis session")
        if sessions:
            ids = [s["session_id"] for s in sessions]
            labels = {s["session_id"]: f"{s['name']} ({s['doc_count']} docs)" for s in sessions}
            index = ids.index(current) if current in ids else 0
            chosen = st.selectbox("Active session", ids, index=index, format_func=labels.get,
                                  label_visibility="collapsed")
            if chosen != current:
                set_active_session(chosen)
                current = chosen
        else:
            st.caption("No sessions yet. Create one on the Home page.")
        st.divider()
        st.caption("Evidence-checked requirements, issues, stories and exports.")
    return current


def require_session() -> str:
    sid = session_sidebar()
    if not sid:
        st.info("Create or select an analysis session on the **Home** page first.")
        st.page_link("Home.py", label="Go to Home", icon="🏠")
        st.stop()
    return sid


def render_warnings(warnings: list[ExtractionWarning]) -> None:
    order = {WarningSeverity.BLOCKING: 0, WarningSeverity.WARNING: 1, WarningSeverity.INFO: 2}
    for w in sorted(warnings, key=lambda x: order[x.severity]):
        text = f"{_SEVERITY_ICON[w.severity]} **{w.message}**" + (f"  \n{w.action}" if w.action else "")
        if w.severity == WarningSeverity.BLOCKING:
            st.error(text)
        elif w.severity == WarningSeverity.WARNING:
            st.warning(text)
        else:
            st.info(text)


def status_badge(status: str) -> str:
    return {"CONFIRMED": "✅ Confirmed", "EXTRACTED": "🕒 Needs review", "FAILED": "⛔ Failed"}.get(status, status)


# ---------------------------------------------------------------- reviewer name (one place, remembered)
def _settings_file():
    return get_settings().data_dir / "settings.json"


def reviewer_input() -> str | None:
    """Sidebar 'Reviewer name', shared by every page and remembered in data/settings.json."""
    import json
    path = _settings_file()
    if "reviewer_name" not in st.session_state:
        try:
            st.session_state["reviewer_name"] = json.loads(path.read_text(encoding="utf-8")).get("reviewer_name", "")
        except (OSError, ValueError):
            st.session_state["reviewer_name"] = ""

    def _save() -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        data["reviewer_name"] = (st.session_state.get("reviewer_name") or "").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    with st.sidebar:
        st.text_input("Reviewer name", key="reviewer_name", placeholder="Your name (audit trail)", on_change=_save)
    return (st.session_state.get("reviewer_name") or "").strip() or None


# ---------------------------------------------------------------- documents changed since last run
def documents_changed(repo, session_id: str, run) -> list[str]:
    """Human-readable differences between the session's usable documents and those the run analysed."""
    from ria.models.enums import ExtractionStatus
    if run is None:
        return []
    docs = {d.document_id: d for d in repo.list_documents(session_id) if d.status != ExtractionStatus.FAILED}
    used = set(run.summary.documents)
    out = [f"added {d.file_name}" for i, d in docs.items() if i not in used]
    out += [f"removed {i}" for i in used if i not in docs]
    types = run.summary.document_types
    out += [f"type changed for {d.file_name}" for i, d in docs.items()
            if i in used and i in types and types[i] != d.document_type.value]
    return out


def run_label(r) -> str:
    """Unique label without AI provider/model (date, time, mode, status, and run ID)."""
    return (f"{r.created_at:%d %b %Y %H:%M:%S} · {r.input_mode.value} · "
            f"{r.status.value} · {r.run_id[:6]}")


def run_picker(repo, session_id: str, key: str):
    """Analysis-run selector; shown only when the session has more than one run. Returns the run or None."""
    runs = repo.list_runs(session_id)
    if not runs:
        return None
    if len(runs) == 1:
        return runs[0]
    labels = {r.run_id: run_label(r) for r in runs}
    chosen = st.selectbox("Analysis run", list(labels), format_func=labels.get, key=key)
    return next(r for r in runs if r.run_id == chosen)


EVIDENCE_ICON = {"STATED": "🟢 Stated", "PROPOSED": "🟡 Proposed", "INFERRED": "🔵 Inferred (AI)",
                 "AMBIGUOUS": "🟠 Ambiguous", "CONFLICTING": "🔴 Conflicting", "UNSUPPORTED": "⚫ Unsupported"}


def evidence_label(status: str) -> str:
    return EVIDENCE_ICON.get(status, status)


def show_segment(seg, file_name: str) -> None:
    loc = seg.location
    st.markdown(f"`{seg.segment_id}` · **{file_name}**" + (f" · {loc}" if loc else ""))
    st.markdown(f"> {seg.text}")
