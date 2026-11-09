import time
from typing import Any
from openai import AsyncOpenAI
from .config import Settings

class ModelNotConfigured(RuntimeError):
    pass

class LLMClient:
    """Single shared OpenAI-compatible client for Ollama Cloud or local Ollama."""
    def __init__(self, settings: Settings):
        self.settings = settings
        api_key = settings.ollama_api_key or ("ollama" if settings.is_local_ollama else "missing-key")
        self.client = AsyncOpenAI(
            base_url=settings.ollama_base_url,
            api_key=api_key,
            timeout=settings.ollama_timeout_seconds,
            max_retries=1,
        )

    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   tool_choice: str = "auto", temperature: float = 0.1, max_tokens: int = 4096):
        if not self.settings.ollama_api_key and not self.settings.is_local_ollama:
            raise ModelNotConfigured("OLLAMA_API_KEY is missing. Add your Ollama Cloud API key to backend/.env.")
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.settings.ollama_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        response = await self.client.chat.completions.create(**kwargs)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        choice = response.choices[0]
        usage = response.usage
        return {
            "message": choice.message,
            "finish_reason": choice.finish_reason,
            "elapsed_ms": elapsed_ms,
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
