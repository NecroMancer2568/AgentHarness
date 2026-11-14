import json
import time
from typing import Any
from .llm import LLMClient
from .repository import ToolRuntime

ROLE_PROMPTS = {
  "coordinator": (
      "You are the coordinator of a multi-agent software engineering team. Turn the user's task into a concise, adaptive execution plan. "
      "Do not edit code. Return valid JSON only with keys: summary (string), steps (array of short strings), likely_files (array of strings), "
      "include_test_scout (boolean), include_security_review (boolean), and risks (array of strings). "
      "Orchestrate the adaptive workflow: identify whether dependency manifests or test suites exist. Prevent unnecessary duplicate analysis and repeated dependency installations. "
      "For bug-fixing or implementation tasks, likely_files MUST include the actual application source code files to modify, not just test files. "
      "Set include_test_scout true unless the task is strictly documentation-only. "
      "Set include_security_review true ONLY when the task involves auth, permissions, secrets, credentials, command execution, SQL injection, or sensitive user data; otherwise false."
  ),
  "repository_analyst": (
      "You are the repository analyst. Investigate the real repository and report:\n"
      "1. Architecture and directory layout.\n"
      "2. Dependency infrastructure (manifests present or inferred imports, packaging format).\n"
      "3. Test infrastructure (frameworks, test conventions, configuration files).\n"
      "4. Critical modules relevant to the task and their expected behavior.\n"
      "Do not edit files. Use tools (list_files, read_file, search_text) to inspect the code; never guess about unseen files. "
      "Return a concise, evidence-based report highlighting key files, root causes, and reproduction steps."
  ),
  "test_scout": (
      "You are the test discovery agent. Inspect the repository to:\n"
      "1. Identify the language and test framework (use discover_tests, list_files, or search_text).\n"
      "2. Discover any existing tests and test directories (including nonstandard locations such as test/, spec/, test_suite/).\n"
      "3. Identify the exact test command (e.g., 'pytest -q tests/test_foo.py', 'npm test', etc.).\n"
      "4. Identify coverage gaps relevant to the suspected bug or task.\n"
      "5. If the repository has NO tests or tests for the bug are missing, explicitly report: 'NO_EXISTING_TESTS: true'.\n"
      "   In that case, outline the exact structure of a meaningful regression test for the Coding Agent to create: "
      "target test directory, test file name (e.g. tests/test_<feature>.py), boundary conditions, invalid inputs, failure cases, and required mocks for external I/O.\n"
      "Do not modify files or run commands. Return concrete, structured findings."
  ),
  "coding_agent": (
      "You are the implementation agent. Implement the user's requested change in the isolated repository.\n"
      "CRITICAL MANDATORY RULES:\n"
      "1. For any bug-fixing or feature-implementation task, you MUST use write_file to modify the actual production code file(s) (e.g., in app/ or src/).\n"
      "   Submitting a test-only change or reporting a bug fix without modifying production code is STRICTLY PROHIBITED and will cause immediate failure.\n"
      "2. Order of operations:\n"
      "   a. Read relevant files to locate the defect and trace callers.\n"
      "   b. Call write_file to modify the production code with the actual fix.\n"
      "   c. Call write_file to add or update meaningful regression tests (e.g. in tests/test_*.py) that specifically test the bug, boundary conditions, and edge cases, and would fail on unpatched code.\n"
      "3. If the repository had NO existing tests, you MUST generate a complete, working regression test file exercising the modified behavior.\n"
      "4. Mock external dependencies (databases, networks, APIs) where appropriate so tests run hermetically.\n"
      "5. Never create trivial tests that merely assert True or pass without exercising the fix. Assert the exact expected behavior.\n"
      "6. Never claim a file was modified unless you actually executed write_file for that file. Inspect your actual edits before summarizing."
  ),
  "test_debug_agent": (
      "You are the test and debugging agent. Inspect the changes, run the configured test command using run_tests, and diagnose failures.\n"
      "1. Check Test Scout findings and locate relevant test files (existing or newly added by Coding Agent).\n"
      "2. Run tests using run_tests with the appropriate command or 'auto'.\n"
      "3. Distinguish environment/dependency problems from genuine code defects.\n"
      "4. If pytest reports exit code 5 (no tests collected) or collection errors, diagnose test file naming, imports, or paths and fix them using write_file.\n"
      "5. If the repository lacks tests, generate or complete the regression test file in the appropriate test directory using write_file.\n"
      "6. Verify via git_diff that tests genuinely exercise the production code changes made by the Coding Agent, rather than merely verifying pre-existing behavior.\n"
      "7. You may fix clear, directly related test/code issues using write_file. Do not weaken or delete tests just to get a pass.\n"
      "8. Report actual command output, collected test count, pass/fail status, and verified evidence."
  ),
  "code_reviewer": (
      "You are an independent code reviewer. Review the task, changes, and test evidence for correctness, regressions, security issues, and maintainability.\n"
      "MANDATORY REVIEW RULES:\n"
      "1. Inspect the git diff provided in the prompt context. If no changes were made, reject with NEEDS_CHANGES.\n"
      "2. If the task requires a bug fix or feature implementation, you MUST verify that production code files (e.g. in app/ or src/) were modified. If only test files or metadata files were modified, REJECT with NEEDS_CHANGES and state rejection reason: 'No production code changes were made; only test/metadata files were modified.'\n"
      "3. Verify that the git diff contains the actual modifications claimed in the implementation summary. If the summary claims a file was edited but the diff shows no changes to that file, REJECT with NEEDS_CHANGES.\n"
      "4. Inspect both production changes and generated tests. Verify test relevance and quality: ensure tests genuinely exercise the fix, test edge cases/boundary conditions, and do not make fabricated claims.\n"
      "5. Check that verification claims match actual execution results. If tests failed or were not collected, reject.\n"
      "6. The complete git diff, changed files list, and test execution results are already provided directly in your review prompt. Do not call any tools. Evaluate the provided git diff and test evidence directly and output your final review verdict immediately.\n"
      "7. End your review with a structured JSON block:\n"
      "```json\n"
      "{\n"
      "  \"verdict\": \"APPROVE\" or \"NEEDS_CHANGES\",\n"
      "  \"summary\": \"Brief review summary\",\n"
      "  \"task_alignment\": \"How changes address original task\",\n"
      "  \"test_evidence_verified\": true or false,\n"
      "  \"rejection_reasons\": [\"reason 1\", ...]\n"
      "}\n"
      "```\n"
      "Or end with explicit lines:\n"
      "VERDICT: APPROVE or VERDICT: NEEDS_CHANGES\n"
      "REJECTION_REASONS:\n"
      "- <reason>"
  ),
  "security_reviewer": (
      "You are a security reviewer for a real software repository. Inspect the task, provided git diff, and changed files for input validation, authorization, secrets, "
      "unsafe command execution, path traversal, injection, and sensitive data exposure. Do not call tools; evaluate the provided evidence directly. "
      "Only report actionable findings supported by evidence. End with VERDICT: APPROVE or VERDICT: NEEDS_CHANGES."
  )
}

ROLE_TOOLS = {
  "repository_analyst": {"list_files", "read_file", "search_text"},
  "test_scout": {"discover_tests", "list_files", "read_file", "search_text"},
  "coding_agent": {"discover_tests", "list_files", "read_file", "search_text", "write_file"},
  "test_debug_agent": {"discover_tests", "list_files", "read_file", "search_text", "write_file", "run_tests", "git_diff"},
  "code_reviewer": {"list_files", "read_file", "search_text", "git_diff"},
  "security_reviewer": {"list_files", "read_file", "search_text", "git_diff"},
}

class AgentExecutor:
    def __init__(self, llm: LLMClient, runtime: ToolRuntime, event):
        self.llm, self.runtime, self.event = llm, runtime, event
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "inference_ms": 0, "tool_calls": 0}

    async def run(self, role: str, task: str, context: str = "", max_steps: int = 8, tools_enabled: bool = True, timeout: float | None = None) -> dict[str, Any]:
        self.event("agent_started", f"{role.replace('_', ' ').title()} started", {"role": role})
        role_started = time.perf_counter()
        role_calls = 0
        role_prompt_tokens = 0
        role_completion_tokens = 0
        role_total_tokens = 0
        role_tool_calls = 0
        messages = [
          {"role": "system", "content": ROLE_PROMPTS[role]},
          {"role": "user", "content": f"TASK:\n{task}\n\nCONTEXT FROM OTHER AGENTS:\n{context[:12000] or '(none)'}\n\nRepository workspace: current working directory. Use the available tools for evidence."}
        ]
        tools = self.runtime.tool_definitions(ROLE_TOOLS[role]) if tools_enabled and role in ROLE_TOOLS else None
        final_text = ""
        latest_content = ""
        tool_call_counts: dict[tuple[str, str], int] = {}
        max_duplicates = getattr(self.runtime.settings, "agentharness_max_duplicate_tool_calls", 2)
        max_agent_tool_calls = getattr(self.runtime.settings, "agentharness_max_tool_calls_per_agent", 12)
        consecutive_same_content = 0

        for step in range(max_steps if tools else 1):
            # Check elapsed time against role timeout
            if timeout and (time.perf_counter() - role_started) >= timeout:
                self.event("agent_timeout", f"{role.replace('_', ' ').title()} reached stage timeout ({timeout}s)", {"role": role, "elapsed_s": timeout})
                break

            # If on the final available step or tool budget reached, steer the agent to provide its final synthesis
            is_final_step = ((step == max_steps - 1) or (role_tool_calls >= max_agent_tool_calls)) and (tools is not None)
            current_tools = None if is_final_step else tools
            if is_final_step:
                if role == "code_reviewer":
                    messages.append({
                        "role": "user",
                        "content": (
                            "You have reached the final step. Do not call any tools. "
                            "Output your final review verdict now with either:\n"
                            "VERDICT: APPROVE\nor\nVERDICT: NEEDS_CHANGES\n"
                            "followed by your summary and rejection reasons (if any)."
                        )
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": "You have reached your final step. Synthesize your findings and provide your complete final response now without calling further tools."
                    })

            try:
                result = await self.llm.chat(
                    messages=messages,
                    tools=current_tools,
                    max_tokens=4096 if role != "coordinator" else 1600,
                    temperature=0.1
                )
            except Exception as exc:
                self.event("agent_error", f"{role} model call failed", {"error": str(exc)[:1500]})
                final_text = latest_content or f"{role.replace('_', ' ').title()} encountered a provider error: {exc}"
                break

            self.usage["calls"] += 1
            role_calls += 1
            role_prompt_tokens += result["prompt_tokens"]
            role_completion_tokens += result["completion_tokens"]
            role_total_tokens += result["total_tokens"]
            self.usage["prompt_tokens"] += result["prompt_tokens"]
            self.usage["completion_tokens"] += result["completion_tokens"]
            self.usage["total_tokens"] += result["total_tokens"]
            self.usage["inference_ms"] += result["elapsed_ms"]

            msg = result["message"]
            tool_calls = getattr(msg, "tool_calls", None) or []
            content = msg.content or ""
            if content.strip():
                if content.strip() == latest_content.strip():
                    consecutive_same_content += 1
                else:
                    consecutive_same_content = 0
                latest_content = content.strip()

            if not tool_calls:
                final_text = content
                break

            if role_tool_calls >= max_agent_tool_calls:
                self.event("tool_budget_exceeded", f"{role} tool call budget reached", {"budget": max_agent_tool_calls})
                final_text = content or latest_content or "Tool call budget reached."
                break

            if is_final_step or (not current_tools and tool_calls):
                final_text = content or latest_content
                break

            if consecutive_same_content >= 2:
                final_text = latest_content
                break

            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [tc.model_dump(exclude_none=True) for tc in tool_calls]
            })

            for tc in tool_calls:
                name = tc.function.name
                allowed_names = ROLE_TOOLS.get(role, set())
                if name not in allowed_names:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": f"Refused: tool '{name}' is not available to role '{role}'."})
                    self.event("tool_refused", f"{role} attempted unavailable tool {name}", {"tool": name, "role": role})
                    continue

                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                # Avoid repeated duplicate tool calls across the entire turn
                sig = (name, json.dumps(args, sort_keys=True))
                count = tool_call_counts.get(sig, 0)
                if count >= max_duplicates:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Repeated tool-call loop detected: Tool '{name}' has already been executed {count} times with these arguments in this session. Do not call it again. Synthesize your findings and proceed."
                    })
                    self.event("tool_loop_prevented", f"{role} duplicate tool call prevented for {name}", {"tool": name, "count": count})
                    continue

                if role_tool_calls >= max_agent_tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Tool call budget exceeded for {role} ({max_agent_tool_calls} tool calls). Synthesize your findings with the evidence gathered so far."
                    })
                    self.event("tool_budget_exceeded", f"{role} tool call budget reached", {"tool": name, "budget": max_agent_tool_calls})
                    break

                tool_call_counts[sig] = count + 1

                self.event("tool_started", f"{role} called {name}", {"tool": name, "arguments": args})
                tool_started = time.perf_counter()
                try:
                    output = await self.runtime.execute(name, args)
                except Exception as exc:
                    output = f"Tool error: {type(exc).__name__}: {exc}"

                duration_ms = round((time.perf_counter() - tool_started) * 1000)
                self.usage["tool_calls"] += 1
                role_tool_calls += 1
                self.event("tool_completed", f"{name} completed ({duration_ms} ms)", {"tool": name, "duration_ms": duration_ms, "output_preview": output[:1200]})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output[:self.runtime.settings.agentharness_max_tool_output_chars]})

        if not final_text:
            final_text = latest_content or f"{role.replace('_', ' ').title()} completed its inspection pass."

        role_metrics = {"calls": role_calls, "prompt_tokens": role_prompt_tokens,
                        "completion_tokens": role_completion_tokens, "total_tokens": role_total_tokens,
                        "tool_calls": role_tool_calls, "duration_ms": round((time.perf_counter() - role_started) * 1000)}
        if not hasattr(self, "agent_metrics"):
            self.agent_metrics = {}
        self.agent_metrics[role] = role_metrics
        self.event("agent_completed", f"{role.replace('_', ' ').title()} completed", {
            "role": role, **role_metrics, "output_preview": final_text[:1800]})
        return {"role": role, "output": final_text, "usage": role_metrics}
