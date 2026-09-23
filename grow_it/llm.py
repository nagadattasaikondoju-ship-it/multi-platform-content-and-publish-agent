"""Thin LLM seam so the pipeline can run against Gemini or a fake in tests."""

import os
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_BRIEF_MODEL = os.getenv("GROW_IT_BRIEF_MODEL", "gemini-2.5-pro")
DEFAULT_POST_MODEL = os.getenv("GROW_IT_POST_MODEL", "gemini-2.5-flash")


class LLM(Protocol):
    async def structured(
        self, *, prompt: str, system: str, schema: type[T], model: str, temperature: float
    ) -> T: ...


class GeminiLLM:
    def __init__(self, api_key: str | None = None):
        from google import genai

        self._client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))

    async def structured(self, *, prompt, system, schema, model, temperature):
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
