"""Replaceable OpenAI-compatible transport for contractor model calls."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Meter:
    calls: int = 0
    tokens: int = 0

    def add(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self.calls += 1
        self.tokens += int(input_tokens or 0) + int(output_tokens or 0)


@dataclass(frozen=True)
class ModelSettings:
    provider: str
    model: str
    temperature: float
    base_url: str | None

    @classmethod
    def from_env(cls) -> "ModelSettings":
        model = os.environ.get("AGENT_MODEL")
        if not model:
            raise RuntimeError("AGENT_MODEL is not set")
        base_url = os.environ.get("OPENAI_BASE_URL")
        provider = "openrouter" if base_url and "openrouter.ai" in base_url else "openai"
        return cls(
            provider=provider,
            model=model,
            temperature=float(os.environ.get("AGENT_TEMPERATURE", "0")),
            base_url=base_url,
        )


class OpenAICompatibleCaller:
    """One system prompt plus one user message per contractor bid."""

    def __init__(self, settings: ModelSettings, meter: Meter):
        from openai import OpenAI

        client_args = {}
        if settings.base_url:
            client_args["base_url"] = settings.base_url
        self.client = OpenAI(**client_args)
        self.settings = settings
        self.meter = meter

    def __call__(self, system: str, user: str) -> str:
        response = self.client.chat.completions.create(
            model=self.settings.model,
            temperature=self.settings.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = response.usage
        self.meter.add(
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
        )
        return response.choices[0].message.content or ""
