import unittest
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch
from openai import AuthenticationError, RateLimitError, APIError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function
from openai.types.completion_usage import CompletionUsage

from app.config import Settings
from app.llm import LLMClient, ModelNotConfigured, LLMProviderError
from app.agents import AgentExecutor, ROLE_PROMPTS, ROLE_TOOLS
from app.repository import ToolRuntime


def create_test_settings(**kwargs) -> Settings:
    defaults = {
        "openrouter_api_key": "sk-or-v1-testkey12345",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_model": "test-provider/test-model",
        "openrouter_timeout_seconds": 30,
        "openrouter_max_retries": 2,
        "openrouter_http_referer": "https://agentharness.local",
        "openrouter_title": "AgentHarness Lab Test",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


# ==============================================================================
# 1. Unit Tests: OpenRouter Client & Configuration
# ==============================================================================

class OpenRouterConfigTests(unittest.TestCase):
    def test_openrouter_client_initialization(self):
        settings = create_test_settings()
        llm = LLMClient(settings)
        assert str(llm.client.base_url) == "https://openrouter.ai/api/v1/"
        assert llm.client.api_key == "sk-or-v1-testkey12345"
        assert llm.client.timeout == 30
        assert llm.client.max_retries == 2
        assert llm.client.default_headers.get("HTTP-Referer") == "https://agentharness.local"
        assert llm.client.default_headers.get("X-Title") == "AgentHarness Lab Test"

    def test_openrouter_base_url_normalization(self):
        settings = create_test_settings(openrouter_base_url="https://openrouter.ai/api")
        assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"

        settings2 = create_test_settings(openrouter_base_url="https://openrouter.ai/api/v1/")
        assert settings2.openrouter_base_url == "https://openrouter.ai/api/v1"

    def test_missing_api_key_raises_model_not_configured(self):
        settings = create_test_settings(openrouter_api_key="")
        llm = LLMClient(settings)
        import asyncio
        with self.assertRaises(ModelNotConfigured):
            asyncio.run(llm.chat(messages=[{"role": "user", "content": "hi"}]))

    def test_missing_model_raises_model_not_configured(self):
        settings = create_test_settings(openrouter_model="")
        llm = LLMClient(settings)
        import asyncio
        with self.assertRaises(ModelNotConfigured):
            asyncio.run(llm.chat(messages=[{"role": "user", "content": "hi"}]))


class OpenRouterAsyncClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_chat_successful_response(self):
        settings = create_test_settings()
        llm = LLMClient(settings)

        mock_message = ChatCompletionMessage(role="assistant", content="Hello from OpenRouter")
        mock_choice = Choice(finish_reason="stop", index=0, message=mock_message)
        mock_usage = CompletionUsage(completion_tokens=10, prompt_tokens=25, total_tokens=35)
        mock_response = ChatCompletion(
            id="gen-12345",
            choices=[mock_choice],
            created=123456789,
            model=settings.openrouter_model,
            object="chat.completion",
            usage=mock_usage,
        )

        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response

            res = await llm.chat(messages=[{"role": "user", "content": "Hello"}])
            self.assertEqual(res["message"].content, "Hello from OpenRouter")
            self.assertEqual(res["finish_reason"], "stop")
            self.assertEqual(res["prompt_tokens"], 25)
            self.assertEqual(res["completion_tokens"], 10)
            self.assertEqual(res["total_tokens"], 35)
            self.assertGreaterEqual(res["elapsed_ms"], 0)

            mock_create.assert_awaited_once()
            _, kwargs = mock_create.call_args
            self.assertEqual(kwargs["model"], "test-provider/test-model")

    async def test_openrouter_auth_error_handling(self):
        settings = create_test_settings()
        llm = LLMClient(settings)

        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = AuthenticationError("Invalid API Key", response=MagicMock(status_code=401), body=None)

            with self.assertRaises(ModelNotConfigured):
                await llm.chat(messages=[{"role": "user", "content": "Hello"}])

    async def test_openrouter_rate_limit_error_handling(self):
        settings = create_test_settings()
        llm = LLMClient(settings)

        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = RateLimitError("Rate limit exceeded", response=MagicMock(status_code=429), body=None)

            with self.assertRaises(LLMProviderError):
                await llm.chat(messages=[{"role": "user", "content": "Hello"}])


# ==============================================================================
# 2. Agent Integration Tests
# ==============================================================================

class OpenRouterAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agents_invoke_openrouter_provider(self):
        roles = [
            "coordinator",
            "repository_analyst",
            "test_scout",
            "coding_agent",
            "test_debug_agent",
            "code_reviewer",
            "security_reviewer",
        ]

        for role in roles:
            settings = create_test_settings()
            llm = LLMClient(settings)
            runtime = MagicMock(spec=ToolRuntime)
            runtime.settings = settings
            runtime.tool_definitions.return_value = [{"type": "function", "function": {"name": "read_file", "description": "Read"}}]

            events = []
            def record_event(typ, msg, data):
                events.append((typ, msg, data))

            executor = AgentExecutor(llm, runtime, record_event)

            mock_message = ChatCompletionMessage(
                role="assistant",
                content=f"Report from {role}"
            )
            mock_choice = Choice(finish_reason="stop", index=0, message=mock_message)
            mock_usage = CompletionUsage(completion_tokens=20, prompt_tokens=50, total_tokens=70)
            mock_response = ChatCompletion(
                id=f"gen-agent-{role}",
                choices=[mock_choice],
                created=123456789,
                model=settings.openrouter_model,
                object="chat.completion",
                usage=mock_usage,
            )

            with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_response

                result = await executor.run(role, task="Analyze repository and find bugs", max_steps=2)

                self.assertEqual(result["role"], role)
                self.assertIn(f"Report from {role}", result["output"])
                self.assertEqual(result["usage"]["calls"], 1)
                self.assertEqual(result["usage"]["total_tokens"], 70)

                mock_create.assert_awaited_once()
                _, kwargs = mock_create.call_args
                self.assertEqual(kwargs["messages"][0]["role"], "system")
                self.assertEqual(kwargs["messages"][0]["content"], ROLE_PROMPTS[role])


# ==============================================================================
# 3. Tool-Calling Compatibility Sequence Test
# ==============================================================================

class OpenRouterToolCallingTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_step_tool_calling_sequence(self):
        settings = create_test_settings()
        llm = LLMClient(settings)
        runtime = MagicMock(spec=ToolRuntime)
        runtime.settings = settings
        runtime.tool_definitions.return_value = [{"type": "function", "function": {"name": "read_file", "description": "Read file"}}]
        runtime.execute = AsyncMock(return_value="def bug(): return 42")

        events = []
        def record_event(typ, msg, data):
            events.append((typ, msg, data))

        executor = AgentExecutor(llm, runtime, record_event)

        # Step 1: Model requests tool call
        mock_tool_call = ChatCompletionMessageToolCall(
            id="call_abc123",
            type="function",
            function=Function(
                name="read_file",
                arguments=json.dumps({"path": "app/main.py"})
            )
        )

        step1_message = ChatCompletionMessage(
            role="assistant",
            content="I will inspect app/main.py.",
            tool_calls=[mock_tool_call]
        )
        step1_response = ChatCompletion(
            id="gen-step1",
            choices=[Choice(finish_reason="tool_calls", index=0, message=step1_message)],
            created=123456789,
            model=settings.openrouter_model,
            object="chat.completion",
            usage=CompletionUsage(completion_tokens=15, prompt_tokens=60, total_tokens=75)
        )

        # Step 2: Model receives tool result and produces final answer
        step2_message = ChatCompletionMessage(
            role="assistant",
            content="I inspected the code. The bug is located in bug() on line 1.",
            tool_calls=None
        )
        step2_response = ChatCompletion(
            id="gen-step2",
            choices=[Choice(finish_reason="stop", index=0, message=step2_message)],
            created=123456790,
            model=settings.openrouter_model,
            object="chat.completion",
            usage=CompletionUsage(completion_tokens=25, prompt_tokens=90, total_tokens=115)
        )

        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = [step1_response, step2_response]

            result = await executor.run("repository_analyst", task="Locate defect in app/main.py", max_steps=4)

            self.assertIn("The bug is located in bug()", result["output"])
            self.assertEqual(result["usage"]["calls"], 2)
            self.assertEqual(result["usage"]["tool_calls"], 1)
            self.assertEqual(result["usage"]["total_tokens"], 190)

            runtime.execute.assert_awaited_once_with("read_file", {"path": "app/main.py"})

            event_types = [e[0] for e in events]
            self.assertIn("agent_started", event_types)
            self.assertIn("tool_started", event_types)
            self.assertIn("tool_completed", event_types)
            self.assertIn("agent_completed", event_types)
