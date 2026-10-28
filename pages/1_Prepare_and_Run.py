"""Milestone 1 · Prepare & Run – upload documents, then one click runs the whole AI pipeline.

Input mode is detected from the documents, depth is always Deep (RIA_ANALYSIS_DEPTH=quick in .env for tight
free-tier limits), the glossary is built automatically from the documents and transcript errors are flagged
as findings – so the BA only uploads, checks the document types and presses Run."""
from __future__ import annotations

import streamlit as st

from ria.logging_setup import ctx, get_logger, setup_logging

setup_logging()
log = get_logger("ui.prepare")

from ria.config import SUPPORTED_EXTENSIONS, get_settings  # noqa: E402
from ria.models.enums import (  # noqa: E402
    DOCUMENT_TYPE_LABELS, INPUT_MODE_LABELS, AnalysisMode, DocumentType, ExtractionStatus, InputMode, ProviderName,
    RunStatus,
)
from ria.pipeline.orchestrator import AnalysisOrchestrator, RunOptions, check_input_mode, suggest_input_mode  # noqa: E402
from ria.providers.base import ProviderError  # noqa: E402
from ria.providers.factory import create_provider, describe_providers  # noqa: E402
from ria.services.ingestion import SAMPLE_FILES, IngestionError, suggest_document_type  # noqa: E402
from ria.ui.common import (  # noqa: E402
    documents_changed, get_services, render_warnings, require_session, setup_page,
)

setup_page("Prepare & Run", "🚀")
repo, service = get_services()
sid = require_session()
settings = get_settings()

st.title("🚀 Prepare & Run")
st.caption("Drop your documents, check their type, then run the complete analysis with one click. Text is "
           "extracted locally; nothing is sent to an AI provider until you press Run.")

# ---------------------------------------------------------------- upload (auto-extract + auto-confirm)
ingested: set = st.session_state.setdefault(f"_ingested_{sid}", set())
files = st.file_uploader("📄 Drop workshop transcripts, BRDs, policies or specifications",
                         type=[e.lstrip(".") for e in SUPPORTED_EXTENSIONS], accept_multiple_files=True,
                         key=f"uploader_{sid}")
for f in files or []:
    if f.file_id in ingested:
        continue
    ingested.add(f.file_id)
    try:
        result = service.ingest(sid, f.name, f.getvalue(), suggest_document_type(f.name))
    except IngestionError as exc:
        st.error(f"**{f.name}**: {exc}")
        log.warning("Upload rejected: %s", exc, extra=ctx(sid))
        continue
    if result.duplicate_of:
        st.info(f"**{f.name}** skipped – identical to {result.duplicate_of.file_name}.")
    elif result.document.status == ExtractionStatus.FAILED:
        st.error(f"**{f.name}** could not be read.")
        render_warnings(result.document.warnings)
    else:
        service.confirm_extraction(sid, result.document.document_id)
        st.toast(f"{f.name} extracted ({result.document.stats.segments} segments)", icon="✅")

docs = repo.list_documents(sid)
usable = [d for d in docs if d.status != ExtractionStatus.FAILED]
if not docs:
    st.info("No documents yet. Drop files above, or load the synthetic Northwind sample set.")
    if st.button("Load sample documents"):
        service.load_samples(sid)
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- documents table with Type dropdown
TYPES = list(DocumentType)
st.markdown("**Documents** – the type is detected from the file name; correct it if needed "
            "(a BRD saved as *notes.docx* would otherwise be treated as meeting notes).")
head = st.columns([3.6, 3.2, 1.8, 0.6], vertical_alignment="bottom")
for col, text in zip(head, ["Document", "Type", "Status", ""]):
    col.caption(text)
for d in docs:
    c = st.columns([3.6, 3.2, 1.8, 0.6], vertical_alignment="center")
    c[0].markdown(f"`{d.document_id}` {d.file_name}")
    new_type = c[1].selectbox("Type", TYPES, index=TYPES.index(d.document_type), key=f"type_{d.document_id}",
                              format_func=lambda t: DOCUMENT_TYPE_LABELS[t], label_visibility="collapsed",
                              disabled=d.status == ExtractionStatus.FAILED)
    if new_type != d.document_type:
        service.change_document_type(sid, d.document_id, new_type)
        st.rerun()
    c[2].markdown("⛔ Unreadable" if d.status == ExtractionStatus.FAILED else "✅ Ready")
    if c[3].button("🗑️", key=f"rm_{d.document_id}", help=f"Remove {d.file_name} from this session"):
        repo.delete_document(sid, d.document_id)
        st.rerun()
    if d.status == ExtractionStatus.FAILED:
        with st.expander(f"⛔ Why {d.file_name} cannot be analysed"):
            render_warnings(d.warnings)

# ---------------------------------------------------------------- automatic configuration
infos = {i.name: i for i in describe_providers(settings)}
real = [n for n, i in infos.items() if i.configured and n != ProviderName.MOCK]
unconfigured = [n for n, i in infos.items() if not i.configured]
# Demo is hidden once a real provider works; unconfigured providers stay visible only until a real provider is set up.
choices = real if real else ([ProviderName.MOCK] + unconfigured)
preferred = ProviderName(settings.default_provider) if settings.default_provider in [n.value for n in infos] else None
if st.session_state.get("adv_provider") not in choices:
    st.session_state["adv_provider"] = preferred if preferred in choices else choices[0]
st.session_state.setdefault("adv_tests", 3)
info = infos[st.session_state["adv_provider"]]
if not info.configured:
    st.error(f"{info.label} is not configured. Add {', '.join(info.missing)} to .env and restart the app.")
mode = suggest_input_mode(usable) if usable else InputMode.BRD_ONLY
depth = AnalysisMode.QUICK if settings.analysis_depth == "quick" else AnalysisMode.DEEP

as_confirmed = [d.model_copy(update={"status": ExtractionStatus.CONFIRMED}) for d in usable]
problems = check_input_mode(mode, as_confirmed)
for p in problems:
    st.error(p)
if info.name == ProviderName.MOCK:
    own = [d.file_name for d in usable if d.file_name not in SAMPLE_FILES]
    if own:
        st.warning("The Demo provider only has results for the Northwind sample files; your own documents "
                   f"({', '.join(own[:3])}) will give empty results. Configure a real AI provider in .env.", icon="🧪")
consent = True
if info.sends_data_externally:
    consent = st.checkbox(f"I confirm these documents are synthetic or approved for sending to {info.label}. "
                          "Emails, phone numbers and speaker names are redacted first.")

latest = repo.latest_run(sid)
changes = documents_changed(repo, sid, latest)
if changes:
    st.warning("Documents changed since the last analysis (" + "; ".join(changes[:4]) + ") – run again.", icon="🔄")

col_run, col_demo = st.columns([3, 2])
run_live = col_run.button("⚡ Run Complete AI Analysis", type="primary", use_container_width=True,
                          disabled=bool(problems) or not consent or not usable or not info.configured,
                          help=f"Execute live AI pipeline with {info.label}")
run_demo = col_demo.button("🧪 Test AI (Demo · 0 Tokens)", type="secondary", use_container_width=True,
                           disabled=bool(problems) or not usable,
                           help="Run instant offline simulation using 0 tokens. Perfect for testing and demos.")

if run_live or run_demo:
    use_mock = bool(run_demo)
    service.confirm_all(sid)
    bar = st.progress(0.0, text="Starting…")
    try:
        from ria.providers.mock import MockProvider
        provider = MockProvider() if use_mock else create_provider(info.name, settings)
        is_external = info.sends_data_externally if not use_mock else False
        run = AnalysisOrchestrator(repo, settings, provider).run(
            sid, RunOptions(mode, depth, redact=is_external,
                            redact_names=is_external, use_cache=True, find_issues=True,
                            generate_stories=True, max_tests_per_story=st.session_state["adv_tests"]),
            lambda msg, frac: bar.progress(min(max(frac, 0.0), 1.0), text=msg))
    except (ProviderError, ValueError) as exc:
        log.error("Run could not start: %s", exc, extra=ctx(sid))
        st.error(str(exc))
        st.stop()
    st.session_state[f"_last_run_{sid}"] = run.run_id
    st.rerun()

last = repo.get_run(st.session_state.get(f"_last_run_{sid}", "")) or latest
if last:
    s = last.summary
    if last.status == RunStatus.FAILED:
        st.error("Last run failed: " + "; ".join(s.errors[:3]))
    elif last.status == RunStatus.COMPLETED_WITH_ERRORS:
        st.warning(f"Last run completed with {s.failed_calls} failed call(s). Run again – successful calls are cached.")
        for err in s.errors[:2]:
            st.caption(f"⚠️ {err}")
    else:
        demo_tag = " · 🧪 Test / Demo Mode (0 Tokens)" if last.is_demo else ""
        st.success(f"Analysis complete{demo_tag} · {last.created_at:%d %b %H:%M}")
    m = st.columns(4)
    m[0].metric("Requirements", s.requirements)
    m[1].metric("Issues found", s.findings)
    m[2].metric("User stories", s.stories)
    m[3].metric("QA tests", s.scenarios)
    n1, n2 = st.columns(2)
    n1.page_link("pages/2_Review_and_Analysis.py", label="Next: review requirements & conflicts", icon="🔎",
                 use_container_width=True)
    n2.page_link("pages/3_Stories_and_Tests.py", label="Stories & tests", icon="📝", use_container_width=True)

# ---------------------------------------------------------------- advanced (provider + tests per story only)
with st.expander("⚙️ Advanced settings"):
    st.selectbox("AI provider", choices, key="adv_provider",
             format_func=lambda n: infos[n].label if infos[n].configured
             else f"{infos[n].label} – not configured ({', '.join(infos[n].missing)} missing)")
    st.caption(info.notes + ("" if real else " Configure Grok, Ollama or an OpenAI-compatible endpoint in .env to "
                                             "analyse your own documents."))
    if st.button("Test connection"):
        try:
            status = create_provider(info.name, settings).health_check()
            (st.success if status.ok else st.error)(status.message)
            log.info("Provider connection test %s: %s", info.name.value, "ok" if status.ok else "failed")
        except ProviderError as exc:
            st.error(str(exc))
    st.slider("Important QA tests per story (max)", 1, 6, key="adv_tests")
