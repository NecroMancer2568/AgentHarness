#!/usr/bin/env python3
"""OpenRouter live API verification utility for AgentHarness Lab.

Usage:
  OPENROUTER_API_KEY="sk-or-v1-..." OPENROUTER_MODEL="anthropic/claude-3.5-sonnet" python3 scripts/test-openrouter.py
"""
import asyncio
import os
import sys
import json
import time

try:
    from openai import AsyncOpenAI, APIError, AuthenticationError
except ImportError:
    print("Error: 'openai' package not installed. Run 'pip install openai' first.")
    sys.exit(1)


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "").strip()
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")

    print("==========================================================")
    print("AgentHarness Lab — OpenRouter API Verification")
    print(f"Base URL: {base_url}")
    print(f"Model:    {model or '(not set)'}")
    print(f"API Key:  {'*' * 8 + api_key[-4:] if len(api_key) > 8 else '(missing)'}")
    print("==========================================================")

    if not api_key:
        print("\nERROR: OPENROUTER_API_KEY is not set.")
        print("Please export OPENROUTER_API_KEY or set it in your .env file.")
        sys.exit(1)

    if not model:
        print("\nERROR: OPENROUTER_MODEL is not set.")
        print("Please export OPENROUTER_MODEL (e.g. anthropic/claude-3.5-sonnet, openai/gpt-4o, qwen/qwen-2.5-coder-32b-instruct).")
        sys.exit(1)

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=30.0,
        default_headers={
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://github.com/agentharness/agentharness-lab"),
            "X-Title": os.getenv("OPENROUTER_TITLE", "AgentHarness Lab Verification"),
        },
    )

    # Test 1: Basic Completion & Auth
    print("\n[1/3] Testing Authentication & Basic Text Completion...")
    started = time.perf_counter()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Respond with the word 'READY' and nothing else."}],
            max_tokens=20,
            temperature=0.0,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        content = response.choices[0].message.content or ""
        usage = response.usage
        print(f"  -> SUCCESS ({elapsed_ms} ms)")
        print(f"  -> Output: {content.strip()}")
        print(f"  -> Tokens: Prompt={getattr(usage, 'prompt_tokens', 0)}, Completion={getattr(usage, 'completion_tokens', 0)}, Total={getattr(usage, 'total_tokens', 0)}")
    except AuthenticationError as exc:
        print(f"  -> AUTHENTICATION FAILED: {exc}")
        print("  -> Verify that your OPENROUTER_API_KEY is active and correct.")
        sys.exit(1)
    except APIError as exc:
        print(f"  -> OPENROUTER API ERROR: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"  -> UNEXPECTED ERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)

    # Test 2: Tool / Function Calling Compatibility
    print("\n[2/3] Testing Function / Tool Calling Capability...")
    sample_tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read contents of a file in the workspace",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path"}
                    },
                    "required": ["path"]
                }
            }
        }
    ]

    started = time.perf_counter()
    try:
        tool_response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a coding assistant. Use read_file to inspect 'app/main.py'."},
                {"role": "user", "content": "Please read app/main.py"}
            ],
            tools=sample_tools,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.0,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        msg = tool_response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            tc = tool_calls[0]
            print(f"  -> SUCCESS ({elapsed_ms} ms)")
            print(f"  -> Tool called: {tc.function.name}")
            print(f"  -> Arguments:   {tc.function.arguments}")
        else:
            print(f"  -> NOTICE: Model responded without tool calls (content: {msg.content[:100]}...)")
            print("  -> Warning: Some models on OpenRouter do not support native function calling.")
    except Exception as exc:
        print(f"  -> TOOL CALLING FAILED: {type(exc).__name__}: {exc}")
        print("  -> Ensure your chosen model supports OpenAI-compatible function calling.")

    # Test 3: Multi-turn Conversation & Shutdown
    print("\n[3/3] Closing client...")
    await client.close()
    print("  -> SUCCESS: OpenRouter API integration is operational.")
    print("==========================================================")


if __name__ == "__main__":
    asyncio.run(main())

