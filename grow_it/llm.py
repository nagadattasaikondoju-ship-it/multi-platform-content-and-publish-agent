"""Thin LLM seam so the pipeline can run against Gemini or a fake in tests."""

import asyncio
import os
import random
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


# Read at call time so a .env loaded by the CLI takes effect.
# Flash works on the free tier; Pro models have zero free quota.
def brief_model() -> str:
    return os.getenv("GROW_IT_BRIEF_MODEL", "gemini-flash-latest")


def post_model() -> str:
    return os.getenv("GROW_IT_POST_MODEL", "gemini-flash-latest")


def fallback_models() -> list[str]:
    raw = os.getenv("GROW_IT_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-flash-lite-latest")
    return [m.strip() for m in raw.split(",") if m.strip()]


RETRYABLE_CODES = {429, 500, 503, 504}
MAX_RETRIES = 6
RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


class LLM(Protocol):
    async def structured(
        self, *, prompt: str, system: str, schema: type[T], model: str, temperature: float
    ) -> T: ...


def retry_delay(error: Exception, attempt: int) -> float:
    """Honour the server's suggested delay when it gives one, else back off exponentially."""
    if match := RETRY_DELAY_RE.search(str(error)):
        return float(match.group(1)) + 1
    return min(2**attempt, 30) + random.random()


class GeminiLLM:
    def __init__(self, api_key: str | None = None):
        from google import genai

        self._client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))

    async def structured(self, *, prompt, system, schema, model, temperature):
        from google.genai import errors

        models = [model] + [m for m in fallback_models() if m != model]
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            # Rotate to a fallback model when the current one is overloaded.
            current = models[attempt % len(models)] if attempt else model
            try:
                return await self._call(prompt, system, schema, current, temperature)
            except errors.APIError as e:
                if e.code not in RETRYABLE_CODES:
                    raise
                last_error = e
                await asyncio.sleep(retry_delay(e, attempt))
        raise last_error

    async def _call(self, prompt, system, schema, model, temperature):
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        if response.parsed is None:
            return schema.model_validate_json(response.text)
        return response.parsed
