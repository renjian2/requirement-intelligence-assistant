from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ria.config import Settings  # noqa: E402
from ria.persistence.repositories import Repository  # noqa: E402
from ria.services.ingestion import IngestionService  # noqa: E402

SAMPLES = ROOT / "sample_data"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path, max_segment_chars=400, chunk_token_budget=300,
        xai_api_key="", xai_model="", ollama_model="",
        openai_compat_base_url="", openai_compat_model="", openai_compat_api_key="",
    )


@pytest.fixture
def repo(settings) -> Repository:
    return Repository(settings.db_path)


@pytest.fixture
def service(repo, settings) -> IngestionService:
    return IngestionService(repo, settings)


@pytest.fixture
def session_id(repo) -> str:
    return repo.create_session("test")
