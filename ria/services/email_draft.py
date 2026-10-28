"""Outlook-ready executive email for critical findings.

Content: cross-document contradictions (values side by side with source files) and open stakeholder
questions. Internal wording notes ("fast", "user-friendly", transcription typos) are NOT included –
those belong to the BA (Specification Quality bucket), not to the client.

Message format (what makes Outlook happy):
* multipart/alternative with text/plain + text/html, both UTF-8 and **Base64** transfer-encoded
  (no quoted-printable soft breaks that can split tags or produce "=20" artefacts);
* serialised with strict CRLF line endings (compat32 + ``linesep='\\r\\n'``, which unlike ``policy.SMTP``
  does not re-split encoded subjects);
* ``X-Unsent: 1`` → Outlook opens the file as an editable compose window, not a read-only message.

HTML is written for Outlook's Word rendering engine: table attributes, ``bgcolor`` header cells with
``<font>`` colours, fonts repeated on every cell, zero-width wrap points in long file names.
Every value from documents is HTML-escaped. Missing fields degrade to readable placeholders.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from ria.models.analysis import FindingRecord, RequirementRecord, StatementRecord
from ria.models.enums import EvidenceStatus, SEVERITY_ORDER, FindingType, ReviewStatus, Severity
from ria.services.resolution import Bucket, finding_bucket
from ria.services.traceability import clarification_questions
from ria.logging_setup import ctx, get_logger

log = get_logger(__name__)


SUBJECT_TEMPLATE = "[ACTION REQUIRED] Critical Requirements Clarifications & Gaps – {session}"
_CLOSED = (ReviewStatus.REJECTED, ReviewStatus.RESOLVED)
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


@dataclass
class ConflictRow:
    finding_id: str
    title: str
    values: list[tuple[str, str]]          # (stated value, source document names)
    business_impact: str
    qa_impact: str
    question: str
    ask_whom: str


@dataclass
class EmailDraft:
    subject: str
    recipients: list[str]                  # suggested people/roles (names)
    to_addresses: list[str]                # valid e-mail addresses supplied by the user
    html_body: str
    text_body: str
    conflicts: list[ConflictRow] = field(default_factory=list)
    high_issues: list[FindingRecord] = field(default_factory=list)
    questions: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not bool(self.conflicts)


# ---------------------------------------------------------------- helpers
def _e(value) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


def _w(value) -> str:
    """Escaped text with zero-width break points after '_' and '/' (long file names can wrap)."""
    return _e(value).replace("_", "_&#8203;").replace("/", "/&#8203;")


def _as_finding(f) -> FindingRecord:
    return f if isinstance(f, FindingRecord) else FindingRecord.model_validate(f)


def _doc_name(docs: dict, doc_id: str) -> str:
    d = docs.get(doc_id)
    if d is None:
        return doc_id
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return d.get("file_name", doc_id)
    return getattr(d, "file_name", doc_id)


def suggested_recipients(findings: list[FindingRecord], questions) -> list[str]:
    names: list[str] = []
    for raw in [f.ask_whom for f in findings] + [q.ask_whom for q in questions]:
        for part in re.split(r"[,;]", raw or ""):
            part = part.strip()
            if part and part.lower() not in {"business owner", "meeting participants"} and part not in names:
                names.append(part)
    return names or ["Business owner", "Product Owner"]


# ---------------------------------------------------------------- builder
def build_email_draft(*, session_name: str, findings: list, requirements: list[RequirementRecord] | None = None,
                      statements: dict[str, StatementRecord] | None = None, documents: dict | None = None,
                      to_addresses: list[str] | None = None, sender_name: str | None = None,
                      max_questions: int = 25) -> EmailDraft:
    findings = [_as_finding(f) for f in (findings or [])]
    docs = documents or {}
    open_findings = [f for f in findings if f.review_status not in _CLOSED]
    stakeholder = [f for f in findings if finding_bucket(f) != Bucket.QUALITY]

    def doc_names(ids: list[str]) -> str:
        return ", ".join(_doc_name(docs, d) for d in ids) or "source not recorded"

    conflicts: list[ConflictRow] = []
    seen_req_ids: set[str] = set()

    for f in sorted(open_findings, key=lambda f: SEVERITY_ORDER[f.severity]):
        if f.finding_type != FindingType.CONTRADICTION:
            continue
        values = [(v.value, doc_names(v.document_ids)) for v in f.values] or [(f.description, doc_names(f.document_ids))]
        conflicts.append(ConflictRow(f.finding_id, f.title, values,
                                     f.business_impact or "Wrong behaviour could be built",
                                     f.qa_impact or "Test cases cannot be finalised until one value is confirmed",
                                     f.clarification_question or "Which value is correct?",
                                     f.ask_whom or "Business owner"))
        seen_req_ids.update(f.requirement_ids)

    # Also include requirements that have CONFLICTING evidence or open contradictions
    if requirements:
        for r in requirements:
            if r.review_status in _CLOSED or getattr(r.review_status, "value", r.review_status) == "OUT_OF_SCOPE":
                continue
            if r.requirement_id in seen_req_ids:
                continue
            if r.evidence_status == EvidenceStatus.CONFLICTING or getattr(r.evidence_status, "value", r.evidence_status) == "CONFLICTING":
                vals = []
                for sid_stmt in r.source_statement_ids:
                    if statements and sid_stmt in statements:
                        st_obj = statements[sid_stmt]
                        vals.append((st_obj.text, _doc_name(docs, st_obj.document_id)))
                if not vals:
                    vals = [(r.description, doc_names(r.document_ids))]
                q = r.unresolved_questions[0] if r.unresolved_questions else "Which source document specifies the intended rule?"
                ask = ", ".join(r.actors) if r.actors else "Business Owner"
                conflicts.append(ConflictRow(
                    r.requirement_id, r.title, vals,
                    "Conflicting rules between source documents; wrong behaviour could be built",
                    "Test scenarios cannot be finalised until confirmed", q, ask))
                seen_req_ids.add(r.requirement_id)
    high = [f for f in open_findings if f.severity == Severity.HIGH and f.finding_type != FindingType.CONTRADICTION
            and finding_bucket(f) != Bucket.QUALITY]
    questions = clarification_questions(stakeholder, requirements or [], statements or {})[:max_questions]
    recipients = suggested_recipients([f for f in open_findings if finding_bucket(f) != Bucket.QUALITY], questions)
    addresses = [a.strip() for a in (to_addresses or []) if _EMAIL_RE.match(a.strip())]
    subject = SUBJECT_TEMPLATE.format(session=(session_name or "").strip() or "Untitled session")
    html_body = _outlook_html(session_name or "Untitled session", recipients, conflicts, high, questions, sender_name)

    lines = [f"Hello {', '.join(recipients)},", "",
             f"Our requirement review of {session_name or 'Untitled session'} found conflicting requirements that need your decision.",
             "", f"Summary: {len(conflicts)} conflicting requirement(s).", ""]
    if conflicts:
        lines.append("CONFLICTING REQUIREMENTS")
        for c in conflicts:
            lines.append(f"- {c.finding_id} {c.title}")
            lines += [f"    {v}  ({src})" for v, src in c.values]
            lines += [f"    Impact: {c.business_impact} / {c.qa_impact}", f"    Question ({c.ask_whom}): {c.question}"]
        lines.append("")
    else:
        lines += ["No conflicting requirements were found. No action is required.", ""]
    lines += ["Thank you,", sender_name or "Business Analysis Team"]
    return EmailDraft(subject, recipients, addresses, html_body, "\n".join(lines), conflicts, high, questions)


FONT = "font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#222222;"
_TABLE = ('<table border="1" cellpadding="6" cellspacing="0" width="100%" '
          'style="border-collapse:collapse;border:1px solid #BFBFBF;mso-table-lspace:0pt;mso-table-rspace:0pt;">')


def _th(text: str, width: str = "") -> str:
    w = f' width="{width}"' if width else ""
    return (f'<td bgcolor="#1F4E79"{w} style="{FONT}background-color:#1F4E79;border:1px solid #BFBFBF;">'
            f'<font color="#FFFFFF"><b>{text}</b></font></td>')


def _td(content: str, shade: bool = False, nowrap: bool = False) -> str:
    bg = ' bgcolor="#F2F6FA"' if shade else ""
    nw = " nowrap" if nowrap else ""
    return f'<td valign="top"{bg}{nw} style="{FONT}border:1px solid #BFBFBF;">{content}</td>'


def _p(content: str) -> str:
    return f'<p style="{FONT}margin:0 0 10pt 0;">{content}</p>'


def _h(content: str, colour: str = "#1F4E79") -> str:
    return (f'<p style="font-family:Calibri,Arial,sans-serif;font-size:13pt;margin:14pt 0 6pt 0;">'
            f'<font color="{colour}"><b>{content}</b></font></p>')


def _outlook_html(session_name, recipients, conflicts, high, questions, sender_name) -> str:
    parts = [
        '<!DOCTYPE html><html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        f'</head><body style="{FONT}margin:0;padding:0;">',
        _p(f"Hello {_w(', '.join(recipients))},"),
        _p(f"Our requirement review of <b>{_e(session_name)}</b> found conflicting requirements between sources that "
           "need your decision before development and testing can continue. Please reply with answers to the questions below."),
        _p(f"<b>Summary:</b> {len(conflicts)} conflicting requirement(s) found."),
    ]
    if conflicts:
        parts.append(_h("Conflicting Requirements", "#C00000"))
        parts.append(_TABLE + "<tr>" + _th("ID", "9%") + _th("Topic", "17%") + _th("Values and sources", "28%")
                     + _th("Impact", "18%") + _th("Question / ask", "28%") + "</tr>")
        for i, c in enumerate(conflicts):
            vals = "<br>".join(f"<b>{_e(v)}</b><br>{_w(src)}" for v, src in c.values)
            shade = i % 2 == 1
            parts.append("<tr>" + _td(_e(c.finding_id), shade, nowrap=True) + _td(_w(c.title), shade)
                         + _td(vals, shade) + _td(f"{_e(c.business_impact)}<br><i>{_e(c.qa_impact)}</i>", shade)
                         + _td(f"{_w(c.question)}<br><br><b>Ask:</b> {_w(c.ask_whom)}", shade) + "</tr>")
        parts.append("</table>")
    else:
        parts.append(_p("No conflicting requirements were found. No action is required."))
    parts.append("<br>" + _p(f"Thank you,<br>{_e(sender_name or 'Business Analysis Team')}"))
    parts.append('<p style="font-family:Calibri,Arial,sans-serif;font-size:8pt;margin:0;"><font color="#888888">'
                 'Drafted by Requirement Intelligence Assistant. Please review before sending.</font></p>'
                 '</body></html>')
    return "".join(parts)


# ---------------------------------------------------------------- .eml
# compat32 serialisation with CRLF: same strict \r\n line endings as policy.SMTP, but it does not re-fold
# (and re-split) an already correctly encoded Subject header.
CRLF_POLICY = policy.compat32.clone(linesep="\r\n")


def _encode_subject(subject: str):
    """ASCII subjects stay readable; others become one continuous UTF-8 Base64 encoded-word sequence.
    (Python's default folding splits words around non-ASCII characters, and RFC 2047 decoders drop the
    spaces between encoded-words – "Gaps – Exp" would show as "Gaps–Exp".)"""
    return subject if subject.isascii() else Header(subject, "utf-8")


def to_eml(draft: EmailDraft) -> bytes:
    """RFC 5322 unsent draft: multipart/alternative, UTF-8 Base64 parts, CRLF line endings."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = _encode_subject(draft.subject)
    if draft.to_addresses:
        msg["To"] = ", ".join(formataddr(("", a)) for a in draft.to_addresses)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="ria.local")
    msg["X-Unsent"] = "1"
    msg.attach(MIMEText(draft.text_body, "plain", "utf-8"))   # utf-8 charset → Base64 transfer encoding
    msg.attach(MIMEText(draft.html_body, "html", "utf-8"))
    return msg.as_bytes(policy=CRLF_POLICY)                    # CRLF everywhere


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_")[:60] or "draft"


def draft_path(folder: Path, now: datetime | None = None) -> Path:
    """Timestamped name, so an earlier draft still open in Outlook (WinError 32 lock) never blocks a new one."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S_%f")
    return folder / f"Requirements_Critical_Findings_{stamp}.eml"


def _find_outlook_exe() -> str | None:
    """Classic Outlook path from the Windows registry, if installed."""
    try:
        import winreg  # Windows only
    except ImportError:
        return None
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                path = winreg.QueryValue(k, None)
                if path and Path(path.strip('"')).exists():
                    return path.strip('"')
        except OSError:
            continue
    return None


def _open_with_com(draft: EmailDraft, timeout_sec: float = 6.0) -> None:
    """Create the draft inside classic Outlook (no file association, no 'choose an app' dialog)."""
    import concurrent.futures

    def _worker():
        import pythoncom  # pywin32, Windows only
        import win32com.client

        pythoncom.CoInitialize()
        try:
            app = win32com.client.Dispatch("Outlook.Application")
            mail = app.CreateItem(0)
            mail.Subject = draft.subject
            if draft.to_addresses:
                mail.To = "; ".join(draft.to_addresses)
            mail.HTMLBody = draft.html_body
            mail.Display(False)
            try:
                mail.GetInspector.Activate()
            except Exception:
                pass
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_worker)
        future.result(timeout=timeout_sec)


def open_in_outlook(eml: bytes, folder: Path, name: str = "", draft: EmailDraft | None = None,
                    ) -> tuple[bool, str, Path]:
    """Save a timestamped .eml and open it as an editable draft. Returns (opened, message, path).

    Windows order: classic Outlook via COM → ``outlook.exe /eml`` → ``os.startfile`` (default app, may ask
    which app to use). macOS: ``open``. Linux: ``xdg-open``."""
    folder.mkdir(parents=True, exist_ok=True)
    path = draft_path(folder)
    path.write_bytes(eml)
    errors: list[str] = []
    if sys.platform == "win32":
        if draft is not None:
            try:
                _open_with_com(draft)
                log.info("Outlook draft opened via COM: %s", path.name)
                return True, "Draft opened in Outlook. Review it and click Send.", path
            except Exception as exc:  # noqa: BLE001 - pywin32 missing / new Outlook / no Outlook
                errors.append(f"COM: {type(exc).__name__}")
                log.info("Outlook COM not available (%s: %s); trying outlook.exe", type(exc).__name__, exc)
        exe = _find_outlook_exe()
        if exe:
            try:
                subprocess.Popen([exe, "/eml", str(path)])  # noqa: S603
                log.info("Outlook draft opened via outlook.exe /eml: %s", path.name)
                return True, "Draft opened in Outlook. Review it and click Send.", path
            except OSError as exc:
                errors.append(f"outlook.exe: {exc}")
        if hasattr(os, "startfile"):
            try:
                os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
                log.info("Outlook draft opened via default .eml app: %s", path.name)
                return True, ("Draft opened with the default app for .eml files. If Windows asks which app to "
                              "use, choose 'Outlook (classic)' and tick 'Always'."), path
            except OSError as exc:
                errors.append(f"default app: {exc}")
    else:
        import shutil
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(opener):
            try:
                subprocess.Popen([opener, str(path)])  # noqa: S603
                return True, "Draft opened in your default mail app. Review it and click Send.", path
            except OSError as exc:
                errors.append(f"{opener}: {exc}")
        else:
            errors.append("server running in cloud / container environment")
    log.warning("Could not open mail app: %s", "; ".join(errors))
    return False, ("The application is running on a cloud server where desktop Outlook cannot be launched directly. "
                   "Please click **'📥 Download .eml Draft'** to open in your local Outlook, or use **'🌐 Cloud / Web Email Options'** below."), path
