"""OpenAI-compatible Chat Completions provider (any base URL: Azure AI Foundry, OpenRouter,
LM Studio, vLLM, …). Grok reuses this with xAI defaults."""
from __future__ import annotations

from ria.providers.base import ProviderStatus, Usage
from ria.providers.http_provider import (
    FatalProviderError, HttpProvider, ProviderError, first_choice_text,
)
from ria.providers.schema import schema_name


class OpenAICompatibleProvider(HttpProvider):
    name = "openai_compatible"
    sends_data_externally = True

    def _endpoint(self) -> str:
        return "/chat/completions"

    def _request_payload(self, messages, temperature, schema, name, mode) -> dict:
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        est_prompt_tokens = prompt_chars // 4

        # For providers with a token-per-minute ceiling (e.g. Groq free tier with 8,000 TPM and 1,000 OTPM),
        # requesting large completion tokens causes Groq to reject the call with HTTP 413 / 429
        # because (prompt_tokens + max_completion_tokens) exceeds TPM or max_completion_tokens exceeds OTPM limit.
        # RIA structured outputs (statements, requirements, findings, stories, tests) need at most
        # 600 - 950 completion tokens. We safely budget max_completion_tokens to never exceed the ceiling.
        is_groq = "groq.com" in self.base_url or self._api_key.startswith("gsk_")
        if is_groq:
            safe_budget = max(500, min(1000, 5200 - est_prompt_tokens))
        else:
            safe_budget = max(1200, min(3000, 7500 - est_prompt_tokens))

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "max_completion_tokens": safe_budget,
        }
        # For reasoning models (e.g. openai/gpt-oss-120b on Groq), instruct low reasoning effort
        # so the model outputs structured JSON concisely instead of burning 6,000+ tokens
        # in internal chain-of-thought monologue.
        if "groq.com" in self.base_url or any(k in self.model.lower() for k in ("gpt-oss", "o1", "o3", "deepseek-r1", "qwq")):
            payload["reasoning_effort"] = "low"

        if mode == "json_schema" and schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name_from(name), "schema": schema, "strict": True}}
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _parse_response(self, data: dict) -> tuple[str, Usage]:
        text = first_choice_text(data)
        u = data.get("usage") or {}
        if "prompt_tokens" in u:
            return text, Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), estimated=False)
        return text, Usage(0, len(text) // 4, estimated=True)

    def health_check(self) -> ProviderStatus:
        try:
            payload = self._request_payload(
                [{"role": "user", "content": "Reply with the single word OK."}], 0.0, None, "", "text")
            payload["max_tokens"] = 60
            text, _ = self._parse_response(self._post(payload))
            return ProviderStatus(True, f"Connected. Model '{self.model}' replied: {text.strip()[:20]}",
                                  self.name, self.model, sends_data_externally=True)
        except FatalProviderError as exc:
            return ProviderStatus(False, str(exc), self.name, self.model, sends_data_externally=True)
        except ProviderError as exc:
            return ProviderStatus(False, f"Connection test failed: {exc}", self.name, self.model,
                                  sends_data_externally=True)


def schema_name_from(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:64] or "result"


__all__ = ["OpenAICompatibleProvider", "schema_name"]
