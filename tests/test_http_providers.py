"""Grok / OpenAI-compatible / Ollama providers against a fake HTTP server (no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from ria.models.analysis import RequirementSynthesisResult, StatementExtractionResult
from ria.providers.base import ChatMessage, ProviderError
from ria.providers.grok import GrokProvider
from ria.providers.http_provider import ContextTooLongError, FatalProviderError, MAX_RETRIES, StructuredOutputError
from ria.providers.ollama import OllamaProvider
from ria.providers.openai_compatible import OpenAICompatibleProvider
from ria.providers.schema import strict_json_schema

MSG = [ChatMessage("system", "s"), ChatMessage("user", "u")]
GOOD = {"chunk_id": "DOC-001-C01", "statements": [{
    "local_key": "s1", "statement_type": "DECISION", "text": "Use email.", "evidence_status": "STATED",
    "segment_ids": ["DOC-001-S0001"], "evidence_quote": "Email is required.", "speaker": None,
    "possible_transcription_issue": False, "transcription_note": None, "supersedes_local_key": None,
    "is_ai_suggestion": False}]}


def chat_response(content: str, usage=True) -> httpx.Response:
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 40}
    return httpx.Response(200, json=body)


def make(handler, cls=GrokProvider, **kw):
    sleeps = []
    p = cls(base_url="https://api.example/v1", model="test-model", api_key="secret-key",
            transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return p, sleeps


def test_strict_schema_is_flat_and_fully_required():
    schema = strict_json_schema(RequirementSynthesisResult)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"default"' not in text
    item = schema["properties"]["requirements"]["items"]
    assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False
    assert {"type": "null"} in item["properties"]["trigger"]["anyOf"]


def test_grok_json_schema_request_and_usage():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        assert req.url.path == "/v1/chat/completions"
        return chat_response(json.dumps(GOOD))

    p, _ = make(handler)
    resp = p.generate_structured(MSG, StatementExtractionResult)
    assert resp.parsed.statements[0].text == "Use email."
    assert seen["auth"] == "Bearer secret-key"
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert resp.usage.input_tokens == 120 and not resp.usage.estimated


def test_401_is_fatal_and_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    p, sleeps = make(handler)
    with pytest.raises(FatalProviderError, match="authentication"):
        p.generate_structured(MSG, StatementExtractionResult)
    assert len(calls) == 1 and not sleeps


def test_429_retried_with_retry_after_then_succeeds():
    responses = iter([httpx.Response(429, headers={"retry-after": "3"}, text="slow down"),
                      httpx.Response(503, text="busy"), chat_response(json.dumps(GOOD))])
    p, sleeps = make(lambda req: next(responses))
    assert p.generate_structured(MSG, StatementExtractionResult).parsed.chunk_id == "DOC-001-C01"
    assert sleeps == [3.0, 4]


def test_429_gives_up_after_max_retries():
    p, sleeps = make(lambda req: httpx.Response(429, text="quota"))
    with pytest.raises(ProviderError, match="Rate limit") as exc:
        p.generate_structured(MSG, StatementExtractionResult)
    assert exc.value.retryable and len(sleeps) == MAX_RETRIES


def test_context_too_long_detected():
    p, _ = make(lambda req: httpx.Response(400, text="This model's maximum context length is 8192 tokens"))
    with pytest.raises(ContextTooLongError):
        p.generate_structured(MSG, StatementExtractionResult)


def test_falls_back_when_json_schema_unsupported():
    modes = []

    def handler(req):
        body = json.loads(req.content)
        rf = body.get("response_format", {}).get("type", "none")
        modes.append(rf)
        if rf == "json_schema":
            return httpx.Response(400, text="response_format json_schema is not supported for this model")
        assert "JSON Schema" in body["messages"][-1]["content"]  # schema moved into the prompt
        return chat_response("```json\n" + json.dumps(GOOD) + "\n```")

    p, _ = make(handler, cls=OpenAICompatibleProvider)
    resp = p.generate_structured(MSG, StatementExtractionResult)
    assert modes == ["json_schema", "json_object"] and "json_object" in resp.note
    p.generate_structured(MSG, StatementExtractionResult)  # remembers the working mode
    assert modes[-1] == "json_object"


def test_invalid_output_repaired_once():
    bad = dict(GOOD, statements=[dict(GOOD["statements"][0], statement_type="NOT_A_TYPE")])
    responses = iter([chat_response(json.dumps(bad)), chat_response(json.dumps(GOOD))])
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return next(responses)

    p, _ = make(handler)
    resp = p.generate_structured(MSG, StatementExtractionResult)
    assert "repaired" in resp.note and resp.usage.input_tokens == 240
    assert "not valid" in bodies[1]["messages"][-1]["content"]


def test_invalid_output_twice_raises():
    p, _ = make(lambda req: chat_response("not json at all"))
    with pytest.raises(StructuredOutputError):
        p.generate_structured(MSG, StatementExtractionResult)


def test_nulls_from_strict_schema_become_defaults():
    data = dict(GOOD, statements=[dict(GOOD["statements"][0], possible_transcription_issue=None)])
    p, _ = make(lambda req: chat_response(json.dumps(data)))
    assert p.generate_structured(MSG, StatementExtractionResult).parsed.statements[0].possible_transcription_issue is False


def test_ollama_uses_format_schema_and_counts_tokens():
    seen = {}

    def handler(req):
        if req.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "test-model:latest"}]})
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(GOOD)},
                                         "prompt_eval_count": 300, "eval_count": 90})

    p, _ = make(handler, cls=OllamaProvider)
    resp = p.generate_structured(MSG, StatementExtractionResult)
    assert seen["format"]["type"] == "object" and seen["stream"] is False
    assert seen["options"]["temperature"] == 0.1 and "authorization" not in seen
    assert resp.usage.input_tokens == 300
    assert p.health_check().ok


def test_ollama_health_reports_missing_model():
    p, _ = make(lambda req: httpx.Response(200, json={"models": [{"name": "llama3:8b"}]}), cls=OllamaProvider)
    status = p.health_check()
    assert not status.ok and "ollama pull test-model" in status.message


def test_connection_error_is_retryable():
    def handler(req):
        raise httpx.ConnectError("refused")

    p, sleeps = make(handler)
    with pytest.raises(ProviderError, match="could not reach") as exc:
        p.generate_structured(MSG, StatementExtractionResult)
    assert exc.value.retryable and len(sleeps) == MAX_RETRIES


def test_missing_model_rejected():
    with pytest.raises(ProviderError, match="No model"):
        GrokProvider(base_url="https://x", model="", api_key="k")
