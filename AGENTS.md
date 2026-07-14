# Repository instructions for coding agents

## Testing: read this before diagnosing failures

Read [`TESTING.md`](TESTING.md) completely before running, changing, or
diagnosing the test infrastructure.

In particular:

- FastAPI's standard `TestClient` is verified working in this repository's
  Python 3.14 environment.
- `ThreadedASGIClient` in the MCP tests is a compatibility name currently
  aliased to `TestClient`; it is not a custom per-request thread wrapper.
- Do not attribute a stall to Python 3.14, Starlette, HTTPX, AnyIO, or
  `TestClient` until the exact diagnostic in `TESTING.md` has been run.
- Messages such as `Failed to create stream fd: Operation not permitted`
  indicate the execution sandbox may be interfering. Re-run the documented
  diagnostic or test command with the appropriate sandbox escalation before
  changing application or test-client code.
- Run tests as `.venv/bin/python -m pytest`, not bare `pytest`, so imports and
  dependency versions come from the repository environment.

Do not replace `TestClient` with a custom event-loop/thread wrapper unless a
real incompatibility remains after following the fallback procedure in
`TESTING.md`.

