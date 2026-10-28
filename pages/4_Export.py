"""Milestone 4 · Export – traceability and the BA deliverables (Word BRD, review report, Excel, Jira CSV).
Diagnostics (log tail, log download, token totals) are tucked away at the bottom."""
from __future__ import annotations

import streamlit as st

from ria.logging_setup import ctx, get_logger, setup_logging, tail

setup_logging()
log = get_logger("ui.export")

from ria.exporters.bundle import load_bundle  # noqa: E402
from ria.exporters.docx_brd import build_brd, build_review_report  # noqa: E402
from ria.exporters.jira_csv import build_jira_csv  # noqa: E402
from ria.exporters.xlsx import build_workbook  # noqa: E402
from ria.services.traceability import traceability  # noqa: E402
from ria.ui.common import get_services, require_session, run_picker, setup_page  # noqa: E402

setup_page("Export", "📦")
repo, _ = get_services()
sid = require_session()
st.title("📦 Export")
run = run_picker(repo, sid, key="export_run")
if run is None:
    st.info("No analysis yet. Run it on **Prepare & Run** first.")
    st.page_link("pages/1_Prepare_and_Run.py", label="Go to Prepare & Run", icon="🚀")
    st.stop()

bundle = load_bundle(repo, sid, run.run_id)
rows = traceability(bundle.requirements, bundle.stories, bundle.scenarios, bundle.findings, bundle.docs)
gaps = [r for r in rows if r.gaps]

# ---------------------------------------------------------------- traceability
m = st.columns(2)
m[0].metric("Requirements", len(rows))
m[1].metric("User stories", len(bundle.stories))
st.subheader("Traceability matrix")
st.caption("Source document → statement → requirement → story → acceptance criteria → test scenario.")
only_gaps = st.toggle("Show only requirements with coverage gaps")
st.dataframe([{"Requirement": r.requirement_id, "Title": r.title, "Review": r.review_status,
               "Evidence": r.evidence_status, "Documents": r.documents, "Stories": r.stories or None,
               "Criteria": r.criteria or None, "Tests": r.scenarios or None, "Findings": r.findings or None,
               "Gaps": "; ".join(r.gaps) or "✓"} for r in (gaps if only_gaps else rows)],
             hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- downloads
st.subheader("Downloads")
final = st.radio("BRD content", ["Draft – everything not rejected (pending items marked)",
                                 "Final – approved items only"], horizontal=True).startswith("Final")
bundle.final = final
if final and bundle.pending_count:
    st.warning(f"{bundle.pending_count} item(s) are still pending review and will not appear in the main body.")
if run.is_demo:
    st.info("This run used demo data. Exported documents are watermarked 'DEMO DATA'.")
stem = f"{bundle.session_name.replace(' ', '_')[:40]}_{run.run_id}"


def _logged(kind: str):
    return lambda: log.info("Export downloaded: %s", kind, extra=ctx(sid, run.run_id))


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
c1, c2, c3, c4 = st.columns(4)
c1.download_button("📄 BRD (Word)", build_brd(bundle), file_name=f"BRD_{stem}{'_final' if final else '_draft'}.docx",
                   mime=DOCX, use_container_width=True, on_click=_logged("BRD final" if final else "BRD draft"))
c2.download_button("📋 Review report (Word)", build_review_report(bundle), file_name=f"Review_report_{stem}.docx",
                   mime=DOCX, use_container_width=True, on_click=_logged("review report"))
c3.download_button("📊 Excel workbook", build_workbook(bundle), file_name=f"Requirements_{stem}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   use_container_width=True, on_click=_logged("excel workbook"))
c4.download_button("🧩 User stories for Jira (CSV)", build_jira_csv(bundle), file_name=f"Jira_stories_{stem}.csv",
                   mime="text/csv", use_container_width=True, on_click=_logged("jira csv"))
st.caption("Jira CSV: in Jira use *Import issues from CSV*. To create issues directly, use **Stories & Tests → "
           "🧩 Jira push**.")

# ---------------------------------------------------------------- diagnostics
with st.expander("🛠️ Diagnostics"):
    calls = repo.get_llm_calls(sid)
    live = [c for c in calls if not c["cached"]]
    d = st.columns(4)
    d[0].metric("AI calls (session)", len(calls))
    d[1].metric("From cache", len(calls) - len(live))
    d[2].metric("Errors", sum(c["status"] != "OK" for c in calls))
    d[3].metric("Tokens", f"{sum(c['input_tokens'] + c['output_tokens'] for c in live):,}")
    lines = tail(200)
    st.caption("Last 200 lines of the application log (secrets, e-mail addresses and phone numbers are masked; "
               "document text is never logged).")
    st.code("".join(lines) or "(log is empty)", language=None)
    st.download_button("Download log", "".join(tail(10_000)).encode("utf-8"), file_name="ria.log",
                       mime="text/plain")
