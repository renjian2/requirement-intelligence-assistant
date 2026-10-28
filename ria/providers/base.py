"""Provider-independent LLM interface. Business logic depends only on this module."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    """User-facing provider failure. `retryable` tells the pipeline whether to retry."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ProviderNotImplementedError(ProviderError):
    pass


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class RequestMeta:
    """Context about a call: used for logging, caching and Mock fixtures.
    Real providers must not send this to the model."""

    stage: str = ""
    document_id: str | None = None
    file_name: str | None = None
    chunk_id: str | None = None
    citable_segment_ids: tuple[str, ...] = ()
    prompt_version: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True  # True when the provider did not report usage


@dataclass
class LLMResponse(Generic[T]):
    parsed: T | None
    raw_text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    is_demo: bool = False
    note: str = ""


@dataclass
class ProviderStatus:
    ok: bool
    message: str
    provider: str
    model: str = ""
    is_demo: bool = False
    sends_data_externally: bool = False


class LLMProvider(ABC):
    name: str = "base"
    is_demo: bool = False
    sends_data_externally: bool = False

    @abstractmethod
    def generate_structured(self, messages: list[ChatMessage], schema: type[T], *,
                            temperature: float = 0.1,
                            meta: RequestMeta | None = None) -> LLMResponse[T]:
        """Return a response whose `parsed` value is validated against `schema`."""

    @abstractmethod
    def generate_text(self, messages: list[ChatMessage], *, temperature: float = 0.2,
                      meta: RequestMeta | None = None) -> LLMResponse:
        ...

    @abstractmethod
    def health_check(self) -> ProviderStatus:
        ...

    @abstractmethod
    def model_info(self) -> dict:
        ...
