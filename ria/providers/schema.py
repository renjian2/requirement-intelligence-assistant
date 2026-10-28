"""Convert Pydantic models into a portable 'strict' JSON Schema.

Works with OpenAI-style strict structured outputs, xAI and Ollama:
* $refs inlined (small local models handle flat schemas better)
* every property listed in `required`; optional ones become nullable
* additionalProperties = false
* keywords that some providers reject (defaults, min/max constraints, formats) removed -
  Pydantic still validates them after the response arrives
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import BaseModel

_DROP_KEYS = {"default", "title", "minItems", "maxItems", "minLength", "maxLength",
              "minimum", "maximum", "format", "pattern", "examples"}


def _resolve(node: Any, defs: dict) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
            return _resolve(merged, defs)
        return {k: _resolve(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve(v, defs) for v in node]
    return node


def _make_nullable(prop: dict) -> dict:
    if "anyOf" in prop:
        if not any(p.get("type") == "null" for p in prop["anyOf"]):
            prop["anyOf"].append({"type": "null"})
        return prop
    t = prop.get("type")
    if isinstance(t, str) and t not in ("null", "array"):
        if "enum" in prop:
            return {"anyOf": [prop, {"type": "null"}], **({"description": prop["description"]} if "description" in prop else {})}
        prop["type"] = [t, "null"]
    return prop


def _strictify(node: Any, in_properties: bool = False) -> Any:
    """`in_properties`: keys are FIELD NAMES (e.g. "title"), never filtered as JSON-Schema keywords."""
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    if not isinstance(node, dict):
        return node
    if in_properties:
        return {name: _strictify(prop) for name, prop in node.items()}
    node = {k: _strictify(v, in_properties=(k == "properties")) for k, v in node.items() if k not in _DROP_KEYS}
    if node.get("type") == "object" and "properties" in node:
        required = set(node.get("required", []))
        for name, prop in node["properties"].items():
            if name not in required:
                node["properties"][name] = _make_nullable(prop)
        node["required"] = list(node["properties"].keys())
        node["additionalProperties"] = False
    return node


def strict_json_schema(model: type[BaseModel]) -> dict:
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    return _strictify(_resolve(raw, defs))


def schema_name(model: type[BaseModel]) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", model.__name__)[:64]


def drop_nulls_for_defaults(data: Any, model: type[BaseModel]) -> Any:
    """Strict schemas force every key; models return null for empty lists. Replace null with
    the field default so Pydantic validation passes (recursively for nested models)."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for name, field in model.model_fields.items():
        if name not in out:
            continue
        value = out[name]
        if value is None and not field.is_required():
            default = field.get_default(call_default_factory=True)
            if default is not None:
                out[name] = default
            continue
        ann = field.annotation
        inner = getattr(ann, "__args__", None)
        if isinstance(value, list) and inner and isinstance(inner[0], type) and issubclass(inner[0], BaseModel):
            out[name] = [drop_nulls_for_defaults(v, inner[0]) for v in value]
    return out


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def extract_json(text: str) -> Any:
    """Parse JSON from model output, tolerating code fences, surrounding prose, and truncation."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        repaired = _repair_truncated_json(cleaned[start:] if start != -1 else cleaned)
        if repaired is not None:
            return repaired
        raise


def _repair_truncated_json(s: str) -> Any | None:
    """Attempt to recover valid JSON from an output cut off mid-stream by token limit."""
    for cut in range(len(s) - 1, max(0, len(s) - 2000), -1):
        if s[cut] in ("}", "]"):
            candidate = s[:cut + 1].rstrip()
            open_curly = candidate.count("{") - candidate.count("}")
            open_square = candidate.count("[") - candidate.count("]")
            if open_curly >= 0 and open_square >= 0:
                closing = ("]" * open_square) + ("}" * open_curly)
                try:
                    return json.loads(candidate + closing)
                except Exception:
                    continue
    return None
