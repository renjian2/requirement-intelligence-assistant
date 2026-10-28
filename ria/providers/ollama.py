"""Ollama local-model provider (data stays on this machine).

Uses /api/chat with `format` = JSON Schema (Ollama structured outputs). The schema is also
added to the prompt, which helps small models follow it. Note: Ollama's cloud service does
not support structured outputs - use a locally pulled model.
"""
from __future__ import annotations

import json

import httpx

from ria.providers.base import ProviderStatus, Usage
from ria.providers.http_provider import HttpProvider, ProviderError


class OllamaProvider(HttpProvider):
    name = "ollama"
    sends_data_externally = False
    output_modes = ("json_schema", "json_object")

    def _endpoint(self) -> str:
        return "/api/chat"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _request_payload(self, messages, temperature, schema, name, mode) -> dict:
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "options": {"temperature": temperature}}
        if mode == "json_schema" and schema is not None:
            payload["format"] = schema
            msgs = [dict(m) for m in messages]
            msgs[-1]["content"] += ("\n\nRespond with JSON matching this schema:\n"
                                    + json.dumps(schema, separators=(",", ":")))
            payload["messages"] = msgs
        elif mode == "json_object":
            payload["format"] = "json"
        return payload

    def _parse_response(self, data: dict) -> tuple[str, Usage]:
        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise ProviderError(f"Empty response from Ollama model '{self.model}'.")
        if "prompt_eval_count" in data:
            return text, Usage(int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)), estimated=False)
        return text, Usage(0, len(text) // 4, estimated=True)

    def health_check(self) -> ProviderStatus:
        try:
            resp = self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
        except (httpx.HTTPError, ValueError) as exc:
            return ProviderStatus(False, f"Ollama is not reachable at {self.base_url} ({type(exc).__name__}). "
                                         "Install Ollama and make sure it is running.", self.name, self.model)
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if self.model not in names and wanted not in names:
            return ProviderStatus(False, f"Model '{self.model}' is not pulled. Run: ollama pull {self.model}. "
                                         f"Available: {', '.join(names) or 'none'}", self.name, self.model)
        return ProviderStatus(True, f"Ollama running. Model '{self.model}' available locally.", self.name, self.model)
