"""Shared logic for HTTP-based providers: retries, error mapping, JSON parsing and repair.

Error policy (Build Prompt v2):
* 429 / 5xx / timeouts / connection errors -> retry with exponential backoff (max 3 retries)
* 401 / 403 / 404 -> fatal, no retry, clear message (the whole run stops)
* context too long -> ContextTooLongError (pipeline splits the chunk and retries)
* invalid JSON / schema mismatch -> one repair attempt with the validation errors
"""
from __future__ import annotations

import json
import re
import time
from abc import abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from ria.providers.base import (
    ChatMessage, LLMProvider, LLMResponse, ProviderError, RequestMeta, T, Usage,
)
from ria.providers.schema import drop_nulls_for_defaults, extract_json, strict_json_schema
from ria.logging_setup import ctx, get_logger

log = get_logger(__name__)


MAX_RETRIES = 5
_CONTEXT_HINTS = ("context length", "context_length", "maximum context", "too many tokens",
                  "prompt is too long", "context window", "tokens exceed", "input is too long",
                  "reduce your message size", "reduce your message")


_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", re.I)
_TRY_AGAIN = re.compile(r"try again in\s*((?:\d+(?:\.\d+)?\s*(?:ms|h|m|s)\s*)+)", re.I)
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str | None) -> float | None:
    """'7.66s', '450ms', '1m2.5s' or plain seconds '3' -> seconds."""
    if not text or not str(text).strip():
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        parts = _DUR.findall(str(text))
        return sum(float(n) * _UNITS[u.lower()] for n, u in parts) if parts else None


def rate_limit_wait(resp: httpx.Response) -> float | None:
    m = _TRY_AGAIN.search(resp.text or "")
    if m and (v := parse_duration(m.group(1))) is not None:
        return v
    for h in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        if (v := parse_duration(resp.headers.get(h))) is not None:
            return v
    return None


class FatalProviderError(ProviderError):
    """Authentication/permission/model problems - retrying will not help."""


class ContextTooLongError(ProviderError):
    pass


class StructuredOutputError(ProviderError):
    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


def _short(text: str, n: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


class HttpProvider(LLMProvider):
    """Subclasses implement `_request_payload`, `_parse_response` and `health_check`."""

    def __init__(self, *, base_url: str, model: str, api_key: str = "",
                 timeout_s: float = 120.0, min_interval_s: float = 0.0,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        if not model:
            raise ProviderError(f"No model configured for the {self.name} provider. Set it in .env.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._last_call = 0.0
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0), transport=transport)

    # ---- to implement --------------------------------------------------------
    @abstractmethod
    def _endpoint(self) -> str: ...

    @abstractmethod
    def _request_payload(self, messages: list[dict], temperature: float,
                         schema: dict | None, schema_name: str, mode: str) -> dict: ...

    @abstractmethod
    def _parse_response(self, data: dict) -> tuple[str, Usage]: ...

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    # Structured-output modes tried in order; a provider that rejects one falls back to the next.
    output_modes: tuple[str, ...] = ("json_schema", "json_object", "prompt_only")

    # ---- HTTP with retries ---------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval_s > 0:
            wait = self._last_call + self.min_interval_s - time.monotonic()
            if wait > 0:
                self._sleep(wait)
        self._last_call = time.monotonic()

    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}{self._endpoint()}"
        last_error = ""
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._client.post(url, headers=self._headers(), json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: could not reach {self.base_url}"
                log.warning("%s: %s (attempt %d/%d)", self.name, last_error, attempt + 1, MAX_RETRIES + 1)
                if attempt < MAX_RETRIES:
                    self._sleep(min(2 ** (attempt + 1), 30)); continue
                raise ProviderError(last_error + ". Check the URL / your network / that the service is running.",
                                    retryable=True) from exc
            if resp.status_code < 400:
                try:
                    return resp.json()
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"Provider returned non-JSON HTTP body: {_short(resp.text)}") from exc
            body = _short(resp.text)
            body_l = body.lower()
            if resp.status_code in (401, 403, 404):
                log.error("%s: fatal HTTP %d", self.name, resp.status_code)
            if resp.status_code in (401, 403):
                raise FatalProviderError(f"{self.name}: authentication/permission failed (HTTP {resp.status_code}). "
                                         "Check the API key in .env.")
            if resp.status_code == 404:
                raise FatalProviderError(f"{self.name}: endpoint or model '{self.model}' not found (HTTP 404). "
                                         f"Check the base URL and model name. {body}")

            # Check if this is a TPM (Tokens Per Minute) / rate limit error
            is_tpm_error = any(k in body_l for k in ("tpm", "tokens per minute", "rate_limit_exceeded"))
            is_rate_limit = (resp.status_code == 429 or resp.status_code >= 500 or
                             (resp.status_code in (400, 413, 422) and is_tpm_error))

            # If it's a TPM limit, check if the single request itself exceeded the total limit:
            # e.g., "Limit 8000, Requested 9500" -> request can never fit without splitting / chunk reduction
            m_tpm = re.search(r"limit\s*(\d+).*?requested\s*(\d+)", body_l)
            if m_tpm:
                limit_val, req_val = int(m_tpm.group(1)), int(m_tpm.group(2))
                if req_val > limit_val:
                    raise ContextTooLongError(f"Request ({req_val} tokens) exceeds provider throughput ceiling ({limit_val} TPM).")

            # True context window error (model context window exceeded, not per-minute rate limit)
            if not is_rate_limit and resp.status_code in (400, 413, 422) and any(h in body_l for h in _CONTEXT_HINTS):
                raise ContextTooLongError(f"Input too long for model {self.model}.")

            if is_rate_limit:
                last_error = f"HTTP {resp.status_code}: {body}"
                if attempt < MAX_RETRIES:
                    wait = rate_limit_wait(resp) if (resp.status_code in (429, 413) or is_tpm_error) else None
                    backoff_s = wait if (wait is not None and wait >= 1.0) else min(2 ** (attempt + 1), 30)
                    log.warning("%s: HTTP %d (rate limit/TPM), retry %d/%d after %.1f s", self.name, resp.status_code,
                                attempt + 1, MAX_RETRIES, backoff_s)
                    if wait is not None and wait > 90:
                        # For Groq, daily quota (200k TPD) is tracked per-model.
                        # If gpt-oss-120b daily quota is exhausted, seamlessly fallback to qwen/qwen3.8-27b
                        if ("groq.com" in self.base_url or self._api_key.startswith("gsk_")):
                            fallback = "qwen/qwen3.8-27b" if "gpt-oss" in self.model.lower() else "openai/gpt-oss-20b"
                            if self.model != fallback:
                                log.warning("Groq model %s daily quota reached (wait %ds) – seamlessly switching to %s with fresh quota",
                                            self.model, int(wait), fallback)
                                self.model = fallback
                                payload["model"] = fallback
                                continue
                        raise ProviderError(f"{self.name}: rate limit reached – the provider asks to wait "
                                            f"{wait:,.0f}s, usually a daily quota. Try later; cached calls are free.",
                                            retryable=True)
                    self._sleep(backoff_s)
                    continue
                hint = " Rate limit / quota reached - wait and re-run (cached calls are free)." \
                    if (resp.status_code in (429, 413) or is_tpm_error) else ""
                raise ProviderError(f"{self.name}: {last_error}.{hint}", retryable=True)
            raise ProviderError(f"{self.name}: request rejected (HTTP {resp.status_code}): {body}")
        raise ProviderError(last_error or "Unknown provider error", retryable=True)

    # ---- public API ------------------------------------------------------------
    def generate_text(self, messages: list[ChatMessage], *, temperature: float = 0.2,
                      meta: RequestMeta | None = None) -> LLMResponse:
        start = time.perf_counter()
        payload = self._request_payload([m.__dict__ for m in messages], temperature, None, "", "text")
        text, usage = self._parse_response(self._post(payload))
        return LLMResponse(parsed=None, raw_text=text, provider=self.name, model=self.model, usage=usage,
                           latency_ms=int((time.perf_counter() - start) * 1000))

    def generate_structured(self, messages: list[ChatMessage], schema: type[T], *,
                            temperature: float = 0.1,
                            meta: RequestMeta | None = None) -> LLMResponse[T]:
        start = time.perf_counter()
        json_schema = strict_json_schema(schema)
        msgs = [m.__dict__ for m in messages]
        total = Usage(0, 0, estimated=False)
        raw, usage, mode = self._call_with_fallback(msgs, temperature, json_schema, schema)
        _add_usage(total, usage)
        try:
            parsed = _validate(raw, schema)
            note = "" if mode == "json_schema" else f"structured output via {mode}"
        except (ValueError, ValidationError) as first_error:
            # One repair attempt with the validation errors.
            repair = msgs + [
                {"role": "assistant", "content": raw[:6000]},
                {"role": "user", "content": "Your previous output was not valid for the required JSON schema.\n"
                                            f"Errors: {_short(str(first_error), 1500)}\n"
                                            "Return ONLY the corrected JSON object. Do not add any commentary."},
            ]
            raw2, usage2 = self._parse_response(self._post(
                self._request_payload(repair, temperature, json_schema, schema.__name__, mode)))
            _add_usage(total, usage2)
            try:
                parsed = _validate(raw2, schema)
            except (ValueError, ValidationError) as exc:
                raise StructuredOutputError(
                    f"Model output did not match {schema.__name__} after one repair attempt: {_short(str(exc))}",
                    raw_text=raw2) from exc
            raw, note = raw2, "repaired after a schema error"
        return LLMResponse(parsed=parsed, raw_text=raw, provider=self.name, model=self.model, usage=total,
                           latency_ms=int((time.perf_counter() - start) * 1000), note=note)

    def _call_with_fallback(self, msgs: list[dict], temperature: float, json_schema: dict,
                            schema: type[BaseModel]) -> tuple[str, Usage, str]:
        modes = list(self.output_modes)
        if getattr(self, "_preferred_mode", None) in modes:
            modes = modes[modes.index(self._preferred_mode):]
        last: ProviderError | None = None
        for mode in modes:
            payload_msgs = msgs
            if mode != "json_schema":
                payload_msgs = _with_schema_instruction(msgs, json_schema)
            try:
                raw, usage = self._parse_response(self._post(
                    self._request_payload(payload_msgs, temperature, json_schema, schema.__name__, mode)))
                self._preferred_mode = mode
                return raw, usage, mode
            except (FatalProviderError, ContextTooLongError):
                raise
            except ProviderError as exc:
                if exc.retryable:
                    raise
                exc_str = str(exc).lower()
                triggers = ("response_format", "format", "schema", "json_validate_failed",
                            "failed to validate json", "failed_generation", "jsonschema", "reasoning_effort")
                if not any(t in exc_str for t in triggers):
                    raise
                last = exc  # this mode is not supported - try the next one
        raise last or ProviderError("No structured-output mode worked")

    def model_info(self) -> dict:
        return {"provider": self.name, "model": self.model, "base_url": self.base_url,
                "structured_mode": getattr(self, "_preferred_mode", "json_schema")}


def _add_usage(total: Usage, u: Usage) -> None:
    total.input_tokens += u.input_tokens
    total.output_tokens += u.output_tokens
    total.estimated = total.estimated or u.estimated


def _validate(raw: str, schema: type[T]) -> T:
    try:
        data = extract_json(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Output is not valid JSON: {exc}") from exc
    return schema.model_validate(drop_nulls_for_defaults(data, schema))


def _with_schema_instruction(msgs: list[dict], json_schema: dict) -> list[dict]:
    instruction = ("\n\nReturn ONLY a JSON object that matches this JSON Schema exactly "
                   "(no markdown, no commentary):\n" + json.dumps(json_schema, separators=(",", ":")))
    out = [dict(m) for m in msgs]
    out[-1]["content"] = out[-1]["content"] + instruction
    return out


def estimate_usage(messages: list[dict], output: str) -> Usage:
    chars = sum(len(m.get("content", "")) for m in messages)
    return Usage(chars // 4, len(output) // 4, estimated=True)


def first_choice_text(data: dict[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected response shape: {_short(json.dumps(data))}") from exc
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content:
        finish = data["choices"][0].get("finish_reason")
        raise ProviderError(f"Empty response from model (finish_reason={finish}).")
    return content
