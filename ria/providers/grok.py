"""Grok (xAI) provider.

Uses xAI's OpenAI-compatible Chat Completions endpoint with JSON-Schema structured output.
Note: xAI describes Chat Completions as a legacy endpoint (Responses API is primary). It is
still offered and keeps this provider interchangeable with other OpenAI-compatible services.
If xAI removes it, only this file needs to change.

Also seamlessly handles Groq API keys ('gsk_...') if entered under Grok settings by routing
to the Groq OpenAI-compatible endpoint.
"""
from __future__ import annotations

from ria.providers.openai_compatible import OpenAICompatibleProvider


class GrokProvider(OpenAICompatibleProvider):
    name = "grok"
    sends_data_externally = True

    def __init__(self, *, base_url: str, model: str, api_key: str = "", **kwargs):
        # Auto-detect Groq keys (starts with 'gsk_') configured with xAI base URL
        if api_key.startswith("gsk_") and "api.x.ai" in base_url:
            base_url = "https://api.groq.com/openai/v1"
            if model == "qwen3.8-27b":
                model = "qwen/qwen3.8-27b"
        super().__init__(base_url=base_url, model=model, api_key=api_key, **kwargs)
