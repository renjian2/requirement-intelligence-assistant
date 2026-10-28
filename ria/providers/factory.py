"""Create providers by name and describe their configuration status."""
from __future__ import annotations

from dataclasses import dataclass

from ria.config import Settings, mask_secret
from ria.models.enums import ProviderName
from ria.providers.base import LLMProvider, ProviderError
from ria.providers.grok import GrokProvider
from ria.providers.mock import MockProvider
from ria.providers.ollama import OllamaProvider
from ria.providers.openai_compatible import OpenAICompatibleProvider

PROVIDER_LABELS = {
    ProviderName.MOCK: "Demo (Mock) – no API key",
    ProviderName.GROK: "Grok (xAI)",
    ProviderName.OLLAMA: "Ollama – local model",
    ProviderName.OPENAI_COMPATIBLE: "OpenAI-compatible endpoint",
}


@dataclass
class ProviderInfo:
    name: ProviderName
    label: str
    configured: bool
    sends_data_externally: bool
    details: dict[str, str]
    notes: str
    missing: list[str]


def describe_providers(s: Settings) -> list[ProviderInfo]:
    def missing(**fields: str) -> list[str]:
        return [k for k, v in fields.items() if not v]

    grok_missing = missing(XAI_API_KEY=s.xai_api_key, XAI_MODEL=s.xai_model)
    oc_missing = missing(OPENAI_COMPAT_BASE_URL=s.openai_compat_base_url, OPENAI_COMPAT_MODEL=s.openai_compat_model)
    ol_missing = missing(OLLAMA_MODEL=s.ollama_model)
    is_groq = s.xai_api_key.startswith("gsk_")
    grok_base_url = "https://api.groq.com/openai/v1 (Groq Cloud)" if is_groq and "api.x.ai" in s.xai_base_url else s.xai_base_url
    grok_model = ("qwen/qwen3.8-27b" if s.xai_model == "qwen3.8-27b" else s.xai_model) if is_groq else s.xai_model
    grok_note = ("Fast cloud inference via Groq Cloud (detected 'gsk_' API key; routed to api.groq.com)."
                 if is_groq else
                 "Best quality. Paid per token. Sends document text to xAI – use synthetic/approved data and keep redaction on.")
    return [
        ProviderInfo(ProviderName.MOCK, PROVIDER_LABELS[ProviderName.MOCK], True, False,
                     {"Model": "mock-fixtures"},
                     "Pre-written demo results for the bundled Northwind sample files only. "
                     "Requirements are built by simple rules, not AI.", []),
        ProviderInfo(ProviderName.GROK, PROVIDER_LABELS[ProviderName.GROK], not grok_missing, True,
                     {"API key": mask_secret(s.xai_api_key), "Base URL": grok_base_url,
                      "Model": grok_model or "(not set)"},
                     grok_note, grok_missing),
        ProviderInfo(ProviderName.OLLAMA, PROVIDER_LABELS[ProviderName.OLLAMA], not ol_missing, False,
                     {"Base URL": s.ollama_base_url, "Model": s.ollama_model or "(not set)"},
                     "Free and private – data stays on your PC. Quality and speed depend on model size and "
                     "hardware. Use a lower RIA_CHUNK_TOKEN_BUDGET (e.g. 1500) for small models.", ol_missing),
        ProviderInfo(ProviderName.OPENAI_COMPATIBLE, PROVIDER_LABELS[ProviderName.OPENAI_COMPATIBLE],
                     not oc_missing, True,
                     {"API key": mask_secret(s.openai_compat_api_key),
                      "Base URL": s.openai_compat_base_url or "(not set)",
                      "Model": s.openai_compat_model or "(not set)"},
                     "Any service with an OpenAI-style /chat/completions API (e.g. Azure AI Foundry, OpenRouter, "
                     "LM Studio). Note: GitHub Models was retired by GitHub in July 2026.", oc_missing),
    ]


def create_provider(name: str | ProviderName, settings: Settings, transport=None) -> LLMProvider:
    name = ProviderName(name)
    common = {"timeout_s": settings.request_timeout_s, "min_interval_s": settings.min_seconds_between_calls,
              "transport": transport}
    if name == ProviderName.MOCK:
        return MockProvider()
    info = next(p for p in describe_providers(settings) if p.name == name)
    if info.missing:
        raise ProviderError(f"{info.label} is not configured. Set {', '.join(info.missing)} in .env and restart the app.")
    if name == ProviderName.GROK:
        return GrokProvider(base_url=settings.xai_base_url, model=settings.xai_model,
                            api_key=settings.xai_api_key, **common)
    if name == ProviderName.OLLAMA:
        return OllamaProvider(base_url=settings.ollama_base_url, model=settings.ollama_model, **common)
    return OpenAICompatibleProvider(base_url=settings.openai_compat_base_url, model=settings.openai_compat_model,
                                    api_key=settings.openai_compat_api_key, **common)
