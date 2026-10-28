"""Project-wide logging.

    from ria.logging_setup import get_logger, setup_logging
    setup_logging()                      # once per process (idempotent); pages and Home call it
    log = get_logger(__name__)
    log.info("Run finished", extra={"session": sid, "run": run_id})

Files (inside RIA_DATA_DIR, default ./data):
    logs/ria.log      all messages at RIA_LOG_LEVEL and above, rotated at midnight, 14 days kept
    logs/errors.log   WARNING and above, with full tracebacks, rotated at midnight, 14 days kept

Privacy (enforced by a filter on every handler, so no call site can bypass it):
    * API keys / tokens (xai-…, gsk_…, sk-…, Bearer/Basic headers, KEY=value pairs) are masked
    * e-mail addresses and phone numbers are masked
    * call sites log IDs and counts only – never document text, quotes or e-mail bodies
"""
from __future__ import annotations

import logging
import os
import re
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT_LOGGER = "ria"
FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | session=%(session)s run=%(run)s | %(message)s"
BACKUP_DAYS = 14

_lock = threading.Lock()
_configured_dir: Path | None = None

_SECRET_PATTERNS = [
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.I), r"\1 ***"),
    (re.compile(r"\b(xai|gsk|sk|pk|ghp|github_pat)[-_][A-Za-z0-9_\-]{6,}", re.I), r"\1-***"),
    (re.compile(r"\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[=:]\s*\S+", re.I), r"\1=***"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    # Phone numbers: international (+91 98765 43210) or 10-digit local (98765 43210 / 9876543210). Dates,
    # times, IDs and token counts are deliberately not matched.
    (re.compile(r"(?<![\w-])\+\d{1,3}[\s-]?\d[\d\s-]{6,12}\d(?![\w-])"), "[phone]"),
    (re.compile(r"(?<![\w:.,-])\d{5}[\s-]?\d{5}(?![\w:.,-])"), "[phone]"),
]


def mask(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class PrivacyFilter(logging.Filter):
    """Masks secrets/PII in the final message and supplies default session/run fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session = getattr(record, "session", "-") or "-"
        record.run = getattr(record, "run", "-") or "-"
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the app
            msg = str(record.msg)
        record.msg, record.args = mask(msg), None
        return True


class _MaskingFormatter(logging.Formatter):
    """Tracebacks can contain secrets too (e.g. an URL with a token) – mask them as well.
    Only the traceback is masked here; the message is masked by PrivacyFilter, and the timestamp/level/
    module prefix must stay untouched."""

    def formatException(self, ei) -> str:  # noqa: N802 - logging API name
        return mask(super().formatException(ei))


def log_dir() -> Path:
    from ria.config import get_settings
    return get_settings().data_dir / "logs"


def setup_logging(level: str | None = None, directory: Path | None = None, force: bool = False) -> Path:
    """Configure the 'ria' logger once. Re-configures only if the target directory changes (tests)."""
    global _configured_dir
    target = Path(directory) if directory else log_dir()
    with _lock:
        root = logging.getLogger(ROOT_LOGGER)
        if _configured_dir == target and not force and root.handlers:
            return target
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        target.mkdir(parents=True, exist_ok=True)
        lvl = getattr(logging, (level or os.getenv("RIA_LOG_LEVEL", "INFO")).upper(), logging.INFO)
        root.setLevel(lvl)
        root.propagate = False
        fmt = _MaskingFormatter(FORMAT)
        privacy = PrivacyFilter()
        for name, h_level in (("ria.log", lvl), ("errors.log", logging.WARNING)):
            handler = TimedRotatingFileHandler(target / name, when="midnight", backupCount=BACKUP_DAYS,
                                               encoding="utf-8", delay=True)
            handler.setLevel(h_level)
            handler.setFormatter(fmt)
            handler.addFilter(privacy)
            root.addHandler(handler)
        _configured_dir = target
        root.info("Logging started (level %s, directory %s)", logging.getLevelName(lvl), target)
        return target


def get_logger(name: str) -> logging.Logger:
    """Child of the 'ria' logger, e.g. get_logger(__name__) -> 'ria.services.review'."""
    name = name if name.startswith(ROOT_LOGGER) else f"{ROOT_LOGGER}.{name}"
    return logging.getLogger(name)


def ctx(session: str | None = None, run: str | None = None) -> dict:
    """extra=… helper: log.info("…", extra=ctx(sid, run_id))."""
    return {"session": session or "-", "run": run or "-"}


def tail(lines: int = 200) -> list[str]:
    """Last N lines of ria.log (for the Diagnostics expander)."""
    path = (_configured_dir or log_dir()) / "ria.log"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return f.readlines()[-lines:]
