"""Application settings loaded from environment variables / .env.

API keys are read here but never logged or displayed in full.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".txt", ".md", ".docx", ".pdf", ".vtt", ".srt", ".csv", ".xlsx",
)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _default_chunk_budget() -> int:
    env_val = os.getenv("RIA_CHUNK_TOKEN_BUDGET")
    val = 3000
    if env_val:
        try:
            val = int(env_val)
        except ValueError:
            pass
    # For Groq / tight 8,000 TPM models, cap at 1,800 tokens per chunk so prompt + completion fits safely
    key = os.getenv("XAI_API_KEY", "") or os.getenv("OPENAI_COMPAT_API_KEY", "")
    url = os.getenv("XAI_BASE_URL", "") or os.getenv("OPENAI_COMPAT_BASE_URL", "")
    if (key.startswith("gsk_") or "groq.com" in url) and val > 1800:
        return 1800
    return val


def _default_min_seconds() -> float:
    env_val = os.getenv("RIA_MIN_SECONDS_BETWEEN_CALLS")
    val = 0.0
    if env_val:
        try:
            val = float(env_val)
        except ValueError:
            pass
    # Pacing for Groq to allow TPM token bucket continuous refill
    key = os.getenv("XAI_API_KEY", "") or os.getenv("OPENAI_COMPAT_API_KEY", "")
    url = os.getenv("XAI_BASE_URL", "") or os.getenv("OPENAI_COMPAT_BASE_URL", "")
    if (key.startswith("gsk_") or "groq.com" in url) and val < 2.0:
        return 2.0
    return val


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("RIA_DATA_DIR", PROJECT_ROOT / "data")))
    max_upload_mb: int = field(default_factory=lambda: _int("RIA_MAX_UPLOAD_MB", 20))
    max_segment_chars: int = field(default_factory=lambda: _int("RIA_MAX_SEGMENT_CHARS", 1200))
    chunk_token_budget: int = field(default_factory=_default_chunk_budget)
    transcript_overlap_segments: int = field(default_factory=lambda: _int("RIA_TRANSCRIPT_OVERLAP", 2))
    max_table_rows: int = field(default_factory=lambda: _int("RIA_MAX_TABLE_ROWS", 5000))

    evidence_threshold: float = field(default_factory=lambda: float(os.getenv("RIA_EVIDENCE_THRESHOLD", "85")))
    max_group_statements: int = field(default_factory=lambda: _int("RIA_MAX_GROUP_STATEMENTS", 30))
    max_parallel_calls: int = field(default_factory=lambda: max(1, _int("RIA_MAX_PARALLEL_CALLS", 1)))
    request_timeout_s: int = field(default_factory=lambda: _int("RIA_REQUEST_TIMEOUT", 180))
    min_seconds_between_calls: float = field(default_factory=_default_min_seconds)

    # Analysis depth: "deep" (default, highest quality) or "quick" (fewer AI calls for tight free-tier limits).
    analysis_depth: str = field(default_factory=lambda: os.getenv("RIA_ANALYSIS_DEPTH", "deep").strip().lower())
    log_level: str = field(default_factory=lambda: os.getenv("RIA_LOG_LEVEL", "INFO").strip().upper())

    # Provider settings
    default_provider: str = field(default_factory=lambda: os.getenv("RIA_DEFAULT_PROVIDER", "mock"))
    xai_api_key: str = field(default_factory=lambda: os.getenv("XAI_API_KEY", ""), repr=False)
    xai_base_url: str = field(default_factory=lambda: os.getenv("XAI_BASE_URL", "https://api.x.ai/v1"))
    xai_model: str = field(default_factory=lambda: os.getenv("XAI_MODEL", ""))
    openai_compat_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_COMPAT_API_KEY", ""), repr=False)
    openai_compat_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_COMPAT_BASE_URL", ""))
    openai_compat_model: str = field(default_factory=lambda: os.getenv("OPENAI_COMPAT_MODEL", ""))
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", ""))

    # Jira (Cloud: email + API token; Data Center: personal access token, leave JIRA_USER_EMAIL empty)
    jira_base_url: str = field(default_factory=lambda: os.getenv("JIRA_BASE_URL", "").rstrip("/"))
    jira_user_email: str = field(default_factory=lambda: os.getenv("JIRA_USER_EMAIL", ""))
    jira_api_token: str = field(default_factory=lambda: os.getenv("JIRA_API_TOKEN", ""), repr=False)
    jira_project_key: str = field(default_factory=lambda: os.getenv("JIRA_PROJECT_KEY", ""))
    jira_issue_type: str = field(default_factory=lambda: os.getenv("JIRA_ISSUE_TYPE", "Story"))
    jira_simulation: bool = field(
        default_factory=lambda: os.getenv("JIRA_SIMULATION", "true").strip().lower() in ("1", "true", "yes"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ria.sqlite3"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def get_settings() -> Settings:
    return Settings()


def mask_secret(value: str) -> str:
    """Return a masked representation of a secret, e.g. 'xai-…9f2c'."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"
