# AgentHarness Benchmark Fixture

This small, intentionally buggy Python repository is a deterministic smoke-test fixture for AgentHarness Lab. It is not presented as a real upstream GitHub project. Each task has its own focused test file so every benchmark run starts from the same committed baseline and can be scored independently.

Run one task's test from the repository root, for example:

```bash
python -m pytest -q tests/test_is_even.py
```

The baseline is expected to fail. AgentHarness should inspect the code, fix only the requested behavior, and demonstrate that the selected test passes in its disposable clone. The original fixture remains unchanged.
