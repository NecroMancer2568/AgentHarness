# Custom benchmark datasets

Place additional `*.jsonl` benchmark manifests in this directory. They appear in the Benchmark Suite page after restarting the API or refreshing the page. Do not name files `*.example.jsonl`; those are intentionally excluded from the selectable dataset list.

Each non-empty line must be one JSON object with these fields:

- `id`: unique stable task ID.
- `repository`: name of a Git repository already cloned under `workspace/repositories/`.
- `input`: specific task prompt, with explicit expected behavior.
- `test_command`: one of `auto`, `pytest`, `python -m pytest`, `npm test`, `npm run test`, `cargo test`, `go test ./...`, `mvn test`, or `gradle test`.
- `max_agent_steps`: optional integer from 1 to 20 (default 8).
- `category`, `expected_behavior`: optional labels for reporting.

A custom manifest must use repositories you trust. The harness preflights repository names before launching a suite and runs each task in a disposable clone. Test commands execute on the host and are not a hardened sandbox.
