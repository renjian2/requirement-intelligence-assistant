"""Streamlit panels for the Outlook email drafter and the Jira workbench (shared by several pages)."""
from __future__ import annotations

from dataclasses import replace

import streamlit as st

from ria.config import get_settings, mask_secret
from ria.models.enums import EXCLUDED_REVIEW
from ria.services.email_draft import build_email_draft, open_in_outlook, safe_filename, to_eml
from ria.logging_setup import ctx, get_logger
from ria.services.jira_client import JiraClient, JiraConfig, build_issue_payload

log = get_logger("ui.integrations")


def render_outlook_panel(repo, session_id: str, run, key: str = "mail", requirement_ids: set[str] | None = None,
                         scope_label: str = "") -> None:
    """Clarification e-mail. ``requirement_ids`` limits it to the requirements currently shown (filters)."""
    from ria.services.resolution import findings_in_scope
    st.subheader("📧 Clarification email")
    st.caption((scope_label + " " if scope_label else "") + "Nothing is sent automatically – you review and send "
               "the draft from Outlook.")
    session = repo.get_session(session_id) or {"name": "Untitled"}
    c1, c2 = st.columns([3, 2])
    to_raw = c1.text_input("To (optional, separate addresses with ;)", key=f"{key}_to",
                           placeholder="finance.lead@company.com; product.owner@company.com")
    sender = c2.text_input("Signature name", key=f"{key}_sender", placeholder="Business Analysis Team")
    all_reqs = repo.get_requirements(run.run_id)
    draft = build_email_draft(
        session_name=session["name"], findings=findings_in_scope(repo.get_findings(run.run_id), requirement_ids),
        requirements=[r for r in all_reqs if requirement_ids is None or r.requirement_id in requirement_ids],
        statements={s.statement_id: s for s in repo.get_statements(run.run_id)},
        documents={d.document_id: d for d in repo.list_documents(session_id)},
        to_addresses=[a for a in to_raw.split(";") if a.strip()], sender_name=sender or None)
    invalid = [a.strip() for a in to_raw.split(";") if a.strip() and a.strip() not in draft.to_addresses]
    if invalid:
        st.warning("Ignored invalid address(es): " + ", ".join(invalid))
    m = st.columns(2)
    m[0].metric("Conflicting requirements", len(draft.conflicts))
    m[1].metric("Suggested recipients", len(draft.recipients))
    st.markdown(f"**Subject:** {draft.subject}  \n**Suggested recipients:** {', '.join(draft.recipients)}")
    if draft.is_empty:
        st.info("There are no open conflicting requirements – the draft says no action is required.")
    eml = to_eml(draft)
    name = safe_filename(f"RIA_clarifications_{session['name']}")
    if st.button("🚀 Open Directly in Outlook", key=f"{key}_open", type="primary", use_container_width=True):
        ok, message, path = open_in_outlook(eml, get_settings().data_dir / "drafts", name, draft=draft)
        (st.success if ok else st.warning)(f"{message}  \nSaved: `{path}`")
        repo.log(session_id, "INFO" if ok else "WARNING", "email", f"Outlook draft {'opened' if ok else 'saved'}: {path.name}")
        log.info("Clarification email: %d conflict(s), opened=%s", len(draft.conflicts),
                 ok, extra=ctx(session_id, run.run_id))
    t_html, t_text = st.tabs(["Email preview (as Outlook shows it)", "Plain text"])
    with t_html:
        import streamlit.components.v1 as components   # isolated frame: app CSS cannot change the preview
        components.html(draft.html_body, height=560, scrolling=True)
    with t_text:
        st.code(draft.text_body, language=None)


def render_jira_workbench(repo, session_id: str, run, key: str = "jira") -> None:
    settings = get_settings()
    base = JiraConfig.from_settings(settings)
    st.subheader("🧩 Jira workbench")
    st.caption("Creates one Jira issue per user story through the Jira REST API (/rest/api/2/issue). Use only a "
               "Jira project your organisation has approved for this tool.")
    sim = st.toggle("🧪 Simulation / Dry-Run Mode (no network calls)", value=base.simulation or bool(base.missing),
                    key=f"{key}_sim", help="Builds the exact payloads and returns SIM-n keys. Ideal for demos.")
    cfg = replace(base, simulation=sim)
    st.table({"Setting": ["Base URL", "Auth", "Token", "Project", "Issue type"],
              "Value": [cfg.base_url or "(not set)", "Cloud: email + API token" if cfg.is_cloud else "Data Center: PAT",
                        mask_secret(cfg.api_token), cfg.project_key or "(not set)", cfg.issue_type]})
    if not sim and cfg.missing:
        st.error(f"Live mode needs {', '.join(cfg.missing)} in .env. Switch on simulation to demo without Jira.")
    client = JiraClient(cfg)
    if st.button("🔍 Test Jira Connection", key=f"{key}_test"):
        ok, msg = client.test_connection()
        log.info("Jira connection test: %s", "ok" if ok else "failed", extra=ctx(session_id))
        (st.success if ok else st.error)(msg)

    stories = [s for s in repo.get_stories(run.run_id) if s.review_status not in EXCLUDED_REVIEW]
    if not stories:
        st.info("No user stories in this run yet. Use **Regenerate stories & tests** above.")
        return
    reqs = {r.requirement_id: r for r in repo.get_requirements(run.run_id)}
    scenarios = repo.get_scenarios(run.run_id)
    only_approved = st.checkbox("Only approved / edited stories", value=False, key=f"{key}_approved")
    pool = [s for s in stories if not only_approved or s.review_status.value in ("APPROVED", "EDITED")]
    chosen = st.multiselect("Stories to create", [s.story_id for s in pool], default=[s.story_id for s in pool[:3]],
                            format_func=lambda i: f"{i} · {next(s.title for s in pool if s.story_id == i)}",
                            key=f"{key}_pick")
    selected = [s for s in pool if s.story_id in chosen]
    if selected:
        with st.expander(f"Preview payload for {selected[0].story_id}"):
            st.json(build_issue_payload(selected[0], reqs, scenarios, cfg))
    label = "🧪 Simulate creating" if sim else "🚀 Create in Jira"
    if st.button(f"{label} {len(selected)} issue(s)", key=f"{key}_create", type="primary",
                 disabled=not selected or (not sim and bool(cfg.missing))):
        bar = st.progress(0.0, text="Creating issues…")
        results = client.create_stories(
            selected, reqs, scenarios,
            progress=lambda done, total, r: bar.progress(done / total, text=f"{done}/{total} · {r.story_id} → "
                                                                            f"{r.key or 'failed'}"))
        ok = [r for r in results if r.ok]
        for r in results:
            repo.log(session_id, "INFO" if r.ok else "ERROR", "jira",
                     f"{r.story_id} -> {r.key or 'FAILED'}{' (simulated)' if r.simulated else ''}{': ' + r.error if r.error else ''}")
        (st.success if len(ok) == len(results) else st.warning)(f"{len(ok)} of {len(results)} issue(s) created"
                                                               + (" (simulation)" if sim else "") + ".")
        st.dataframe([{"Story": r.story_id, "Result": "✅" if r.ok else "❌", "Jira key": r.key or None,
                       "Link": r.url or None, "Error": r.error or None} for r in results],
                     hide_index=True, width="stretch", column_config={"Link": st.column_config.LinkColumn()})
