# Atlas

Test-runner bot. The first executor in the Lightsei constellation.

## What it does

Claims `atlas.run_tests` commands from Lightsei's dispatch queue, runs `pytest` in a subprocess, parses the summary, emits an `atlas.tests_run` event with structured outcome, and dispatches a `hermes.post` command so the result lands wherever Hermes is configured to deliver it.

Atlas does NOT call Slack or any other notification channel directly. That's Hermes's job. Atlas just produces a clean structured outcome and hands the "tell someone" responsibility off via the dispatch chain — the dispatch_chain_id from the inbound command propagates automatically through the SDK's thread-local context (Phase 11.1).

## Phase 11.3 scope

One inbound command kind, one downstream dispatch, two event types.

| In | Out | Event |
|---|---|---|
| `atlas.run_tests` | `hermes.post` | `atlas.tests_run` (and `atlas.crash` on the failure path) |

Multiple inbound kinds (single-test runs, flaky-test reruns, PR-opening on suggested fixes) are Phase 13+ work.

## Configuration

| Env | Default | What it controls |
|---|---|---|
| `ATLAS_POLL_S` | `5` | seconds between claim attempts |
| `ATLAS_PYTEST_ARGS` | `backend/tests/` | what pytest runs |
| `ATLAS_TEST_DIR` | `.` | working directory for pytest |
| `ATLAS_TIMEOUT_S` | `300` | per-test-run timeout |
| `ATLAS_LOG_TAIL_BYTES` | `4096` | bytes of stdout/stderr to attach to events |
| `ATLAS_HERMES_CHANNEL` | `default` | channel name to pass on the hermes.post payload |

Workspace secret `LIGHTSEI_API_KEY` is required (the worker injects it). The bot exits with code 2 if it's missing rather than dispatching anonymously.

## Deploy

```bash
lightsei deploy ./agents/atlas
```

Or via Phase 10's push-to-deploy: a push that touches `agents/atlas/` files redeploys Atlas automatically once the workspace's GitHub integration is mapped.

## Local test

```bash
cd backend
pytest tests/test_atlas.py
```

Tests use injected mocks for both the `lightsei` client and the pytest runner so the test suite isn't recursively running pytest inside pytest.
