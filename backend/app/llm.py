import time
from typing import Any
from openai import AsyncOpenAI, APIError, AuthenticationError, RateLimitError, APITimeoutError
from .config import Settings

class ModelNotConfigured(RuntimeError):
    pass

class LLMProviderError(RuntimeError):
    pass

class LLMClient:
    """Single shared OpenAI-compatible client for OpenRouter API."""
    def __init__(self, settings: Settings):
        self.settings = settings
        api_key = settings.openrouter_api_key.strip() or "missing-key"

        default_headers: dict[str, str] = {}
        if settings.openrouter_http_referer.strip():
            default_headers["HTTP-Referer"] = settings.openrouter_http_referer.strip()
        if settings.openrouter_title.strip():
            default_headers["X-Title"] = settings.openrouter_title.strip()

        self.client = AsyncOpenAI(
            base_url=settings.openrouter_base_url,
            api_key=api_key,
            timeout=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
            default_headers=default_headers or None,
        )

    async def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   tool_choice: str = "auto", temperature: float = 0.1, max_tokens: int = 4096):
        if not self.settings.openrouter_api_key.strip():
            raise ModelNotConfigured("OPENROUTER_API_KEY is missing. Add your OpenRouter API key to backend/.env or environment variables.")
        if not self.settings.openrouter_model.strip():
            raise ModelNotConfigured("OPENROUTER_MODEL is missing. Set your desired OpenRouter model (e.g., anthropic/claude-3.5-sonnet, openai/gpt-4o, qwen/qwen-2.5-coder-32b-instruct) in backend/.env or environment variables.")

        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.settings.openrouter_model.strip(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except AuthenticationError as exc:
            raise ModelNotConfigured(f"OpenRouter authentication failed: {exc}") from exc
        except (RateLimitError, APITimeoutError, APIError) as exc:
            raise LLMProviderError(f"OpenRouter API error: {exc}") from exc
        except Exception as exc:
            raise LLMProviderError(f"OpenRouter request failed: {exc}") from exc

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
