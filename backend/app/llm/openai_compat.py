"""Hosted tiers: Groq and OpenRouter, both OpenAI-compatible `/chat/completions` endpoints.

JSON mode (`response_format: json_object`) with the schema in the prompt: supported by every
model in the chain, and the gateway validates the answer with Pydantic anyway (rule 6).
Retries are bounded and only for timeouts, connection errors and 5xx (rule 7). A 429 is not
retried here: the gateway moves on to the next tier and cools this one down, because a free
tier's rate limit lasts longer than any sensible wait.
"""

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from app.llm.ollama import LlmError
from scenario_gen.director import LlmReply, Message

log = structlog.get_logger()

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
RETRYABLE_STATUS = {500, 502, 503, 504}


class _Msg(BaseModel):
    content: str | None = None


class _Choice(BaseModel):
    message: _Msg
    finish_reason: str | None = None


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None  # OpenRouter reports it when asked (`usage: {include: true}`)


class _Completion(BaseModel):
    model: str = ""
    choices: list[_Choice]
    usage: _Usage = _Usage()


class OpenAICompatClient:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        *,
        timeout_s: float = 20.0,
        attempts: int = 2,
        max_tokens: int = 800,
        temperature: float = 0.3,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if provider not in BASE_URLS and base_url is None:
            raise ValueError(f"unknown provider {provider!r}")
        self.provider = provider
        self.model = model
        self._url = (base_url or BASE_URLS[provider]).rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = httpx.Timeout(timeout_s, connect=min(5.0, timeout_s))
        self._attempts = attempts
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._transport = transport

    def _body(self, messages: Sequence[Message], schema: dict[str, Any]) -> dict[str, Any]:
        instruction = (
            "Reply with one JSON object only, no prose, matching this JSON schema:\n"
            + json.dumps(schema, separators=(",", ":"))
        )
        msgs = [dict(m) for m in messages]
        if msgs and msgs[0]["role"] == "system":
            msgs[0]["content"] = msgs[0]["content"] + "\n\n" + instruction
        else:
            msgs.insert(0, {"role": "system", "content": instruction})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "response_format": {"type": "json_object"},
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
        if "gpt-oss" in self.model:
            # A reasoning model: its thinking counts against max_tokens. These are short JSON
            # answers, so keep the thinking short (seen live: 800 tokens for a 3-line reply).
            body["reasoning_effort"] = "low"
        return body

    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply:
        body = self._body(messages, schema)
        last: LlmError | None = None
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(1, self._attempts + 1):
                started = time.monotonic()
                try:
                    resp = await client.post(self._url, json=body, headers=self._headers)
                except httpx.TimeoutException:
                    last = LlmError(f"{self.provider} timed out", "timeout")
                except httpx.TransportError as exc:
                    last = LlmError(f"{self.provider} unreachable: {exc!r}", "unavailable")
                else:
                    if resp.status_code == 429:
                        raise LlmError(f"{self.provider} rate limited", "rate_limited")
                    if resp.status_code in RETRYABLE_STATUS:
                        last = LlmError(f"{self.provider} HTTP {resp.status_code}", "unavailable")
                    elif resp.status_code != 200:
                        # 400/401/403/404...: permanent for this tier, never retried (rule 7)
                        raise LlmError(
                            f"{self.provider} HTTP {resp.status_code}: {resp.text[:200]}",
                            "rejected",
                        )
                    else:
                        return self._reply(resp, started)
                log.warning(
                    "llm_retry", provider=self.provider, model=self.model, attempt=attempt,
                    error=str(last),
                )  # fmt: skip
                if attempt < self._attempts:
                    await asyncio.sleep(0.5 * 2 ** (attempt - 1))
        raise last or LlmError("no attempts made", "unavailable")

    def _reply(self, resp: httpx.Response, started: float) -> LlmReply:
        try:
            parsed = _Completion.model_validate_json(resp.content)
        except ValidationError as exc:
            raise LlmError(f"unexpected {self.provider} response: {exc}", "bad_response") from None
        if not parsed.choices or not parsed.choices[0].message.content:
            raise LlmError(f"{self.provider} returned no content", "bad_response")
        return LlmReply(
            content=parsed.choices[0].message.content,
            model=f"{self.provider}/{parsed.model or self.model}",
            input_tokens=parsed.usage.prompt_tokens,
            output_tokens=parsed.usage.completion_tokens,
            latency_ms=round((time.monotonic() - started) * 1000),
            cost_usd=parsed.usage.cost or 0.0,
        )
