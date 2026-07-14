# Testing notes: sync test client against the FastAPI apps

## Use `fastapi.testclient.TestClient`, not a custom thread wrapper

Verified directly in this repo's `.venv` (Python 3.14.4, fastapi 0.138.2,
starlette 0.52.1, httpx 0.27.2, anyio 4.14.1): plain `TestClient` works
against both `profile_os.api:create_app()` and `profile_os.mcp_server`
apps with no stall. There is no real Python 3.14 incompatibility in this
environment — a comment claiming one in `tests/test_mcp_server.py` was
stale/wrong for this venv, and the hand-rolled `ThreadedASGIClient` it
motivated (a fresh `anyio.run()` inside a brand-new
`ThreadPoolExecutor(max_workers=1)` per request) was the actual source of
a stall, not a fix for one. That file now aliases
`ThreadedASGIClient = TestClient` instead of reimplementing it.

```python
import pytest
from fastapi.testclient import TestClient
from profile_os.api import create_app

@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=False)
    with TestClient(app) as c:
        yield c
```

Covers everything a sync test needs out of the box:
- **Context-manager lifespan**: the `with` block runs ASGI `startup`/
  `shutdown` through one persistent background thread + event loop
  (`anyio.from_thread.start_blocking_portal()` internally) — not a new
  loop per call.
- **Redirects**: `client.get(url, follow_redirects=False)` to inspect a
  redirect response directly (used by the OAuth-authorize tests here).
- **JSON body**: `client.post(url, json={...})`.
- **Form body**: `client.post(url, data={...})` (used by the OAuth
  consent-screen and approval-link tests here, which post
  `application/x-www-form-urlencoded`).
- **Sync endpoints + thread-local SQLite**: sync `def` routes run through
  Starlette's `run_in_threadpool` *inside* the one already-running portal
  loop, so `Store`'s `threading.local()`-cached `sqlite3` connections stay
  bound to a small, stable set of threads instead of a new one (and a new
  connection) being opened and leaked on every single request.

Before reaching for anything custom, run the one-line check that
diagnosed this originally, in whatever venv is failing:

```bash
python3 -c "
from fastapi.testclient import TestClient
from profile_os.api import create_app
app = create_app(data_dir='/tmp/pos_diag', auth_enabled=False)
with TestClient(app) as c:
    r = c.get('/profiles')
    print(r.status_code, len(r.json()))
"
```

If that hangs or errors in a *specific* environment, it's worth pinning
`starlette`/`httpx`/`anyio` to match this repo's known-good combination
above before writing a replacement — version drift between those three is
the far more common cause of "TestClient doesn't work" than anything
Python-3.14-specific.

## Fallback: if a real incompatibility is ever confirmed

Only reach for this if the one-liner above genuinely fails after
confirming/pinning versions. The correct fallback is **one persistent
portal per test/fixture**, not a new thread+loop per request — this is
exactly what `TestClient` does internally, so hand-rolling it means
matching that shape, not avoiding it:

```python
import anyio
import pytest
from httpx import ASGITransport, AsyncClient

class _SyncPortalClient:
    """Thin sync facade over one AsyncClient + one blocking portal,
    shared across every request in the fixture's lifetime."""

    def __init__(self, async_client: AsyncClient, portal):
        self._c = async_client
        self._portal = portal

    def get(self, url, **kw):
        return self._portal.call(lambda: self._c.get(url, **kw))

    def post(self, url, **kw):
        return self._portal.call(lambda: self._c.post(url, **kw))

    # add put/patch/delete/request the same way as needed

@pytest.fixture
def client(tmp_path):
    from profile_os.api import create_app
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=False)

    with anyio.from_thread.start_blocking_portal() as portal:
        async def _setup():
            c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            await app.router.startup()
            return c

        async_client = portal.call(_setup)
        try:
            yield _SyncPortalClient(async_client, portal)
        finally:
            portal.call(async_client.aclose)
            portal.call(app.router.shutdown)
```

Key properties that make this safe (and that a per-request thread wrapper
gets wrong):

- **One thread, one event loop, for the whole fixture** — not recreated
  per request, so anything bound to "the" running loop (SQLite
  thread-locals, any `app.state` resource opened at `startup`) stays
  valid across every call in the test.
- **Explicit `startup`/`shutdown`** via `app.router`, since we're not
  using `TestClient`'s context manager to do it for us.
- **`follow_redirects` / `data=` / `json=`** all pass straight through to
  the underlying `httpx.AsyncClient` call, same as the sync client.

Do not replace this per-call with a fresh `ThreadPoolExecutor` +
`anyio.run()` — that recreates the loop (and, via `Store`'s
`threading.local()`, a new never-closed SQLite connection) on every
single request, which is what caused the original stall.
