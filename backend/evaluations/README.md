# Inspect AI tasks

`agentharness_eval.py` wraps the running AgentHarness API as an Inspect AI solver. Inspect owns the evaluation dataset/log/scoring workflow; AgentHarness owns the actual multi-agent run, tools, Git workspace, tests, and execution metrics.

Edit `tasks.jsonl` before running. Each line is one JSON object:

```json
{"id":"bug-001","repository":"my-repository","input":"Fix the documented bug in the parser and add a regression test.","target":"verified_success","test_command":"auto","max_agent_steps":8}
```

- `repository`: repository folder name under `AGENTHARNESS_REPOS_ROOT`.
- `input`: task instructions.
- `target`: descriptive score target; the scorer uses actual run verification.
- `test_command`: `auto` or an allowlisted test command.
- `max_agent_steps`: tool steps allowed per agent.

Run the API and then `inspect eval evaluations/agentharness_eval.py` from `backend/`.
