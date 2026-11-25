"""Local LLM client: Ollama's /api/chat with JSON-schema structured output.

qwen3:8b runs with thinking disabled (`think: false`) so it returns the JSON object directly.
Retries are bounded and only for transient failures — timeouts, connection errors, 429/5xx
(rule 7); anything else (bad request, unknown model, invalid body) fails immediately.
The LLM gateway (B9) uses it as the "local" tier.
"""

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from scenario_gen.director import LlmReply, Message

log = structlog.get_logger()

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class LlmError(Exception):
    """The model could not produce an answer (after any allowed retries)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind  # "timeout" | "unavailable" | "rate_limited" | "bad_response" | "rejected"


class _ChatMessage(BaseModel):
    content: str


class _ChatResponse(BaseModel):
    message: _ChatMessage
    model: str
    prompt_eval_count: int = 0
    eval_count: int = 0


def model_name(litellm_style: str) -> str:
    """'ollama/qwen3:8b' (LiteLLM naming, as in LOCAL_MODEL) -> 'qwen3:8b'."""
    return litellm_style.removeprefix("ollama/").removeprefix("ollama_chat/")


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 180.0,
        attempts: int = 3,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model_name(model)
        self._timeout = httpx.Timeout(timeout_s, connect=10.0)
        self._attempts = attempts
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._transport = transport

    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply:
        options: dict[str, Any] = {"temperature": self._temperature}
        if self._max_tokens is not None:
            options["num_predict"] = self._max_tokens
        body = {
            "model": self.model,
            "messages": list(messages),
            "format": schema,
            "stream": False,
            "think": False,
            "options": options,
        }
        last: LlmError | None = None
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(1, self._attempts + 1):
                started = time.monotonic()
                try:
                    resp = await client.post(f"{self._base_url}/api/chat", json=body)
                except httpx.TimeoutException:
                    last = LlmError("Ollama timed out", "timeout")
                except httpx.TransportError as exc:
                    last = LlmError(f"Ollama unreachable: {exc!r}", "unavailable")
                else:
                    if resp.status_code in TRANSIENT_STATUS:
                        kind = "rate_limited" if resp.status_code == 429 else "unavailable"
                        last = LlmError(f"Ollama HTTP {resp.status_code}", kind)
                    elif resp.status_code != 200:
                        raise LlmError(
                            f"Ollama HTTP {resp.status_code}: {resp.text[:200]}", "rejected"
                        )
                    else:
                        return self._reply(resp, started)
                log.warning("llm_retry", model=self.model, attempt=attempt, error=str(last))
                if attempt < self._attempts:
                    await asyncio.sleep(2 ** (attempt - 1))
        raise last or LlmError("no attempts made", "unavailable")

    def _reply(self, resp: httpx.Response, started: float) -> LlmReply:
        try:
            parsed = _ChatResponse.model_validate_json(resp.content)
        except ValidationError as exc:
            raise LlmError(f"unexpected Ollama response: {exc}", "bad_response") from None
        return LlmReply(
            content=parsed.message.content,
            model=f"ollama/{parsed.model}",
            input_tokens=parsed.prompt_eval_count,
            output_tokens=parsed.eval_count,
            latency_ms=round((time.monotonic() - started) * 1000),
        )
