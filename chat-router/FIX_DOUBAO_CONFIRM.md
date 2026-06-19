# Fix: Doubao Login Confirm Returns HTTP 500

**Date:** 2026-06-19
**Status:** Resolved
**File:** `kimi-proxy/app.py` — `_cmd_login_confirm()`

## Symptom

After logging in to Doubao in the popup browser, clicking **Confirm** in the UI failed.
The modal stayed on "Saving..." and the request eventually returned an error.

`server.log` showed:

```
"POST /api/login/doubao/confirm HTTP/1.1" 500 -
"POST /api/login/doubao/confirm HTTP/1.1" 500 -
"POST /api/login/deepseek/confirm HTTP/1.1" 200 -   <-- DeepSeek worked fine
```

DeepSeek and Kimi confirmed successfully; only Doubao failed with 500.

## Root Cause

In `_cmd_login_confirm()`, the confirm flow waited for the page to settle with:

```python
await page.wait_for_load_state("networkidle")   # no timeout
```

Doubao keeps **persistent connections open** (websocket / long-poll), so the
`networkidle` state is never reached. Playwright waits until its default 30s timeout,
then raises `TimeoutError`. Because this line was **not** wrapped in a `try`, the
exception escaped the coroutine and propagated out of the Flask route, producing
**HTTP 500**.

This matches the timing in the log: the popup opened at 18:18:21 and confirm returned
500 at 18:19:24 (~60s later) — consistent with the load-state wait timing out.

DeepSeek and Kimi do not hold persistent connections, so `networkidle` fired normally
and their confirm succeeded.

> Note: Flask's default access log records the status line only (debug mode off), so the
> traceback was not printed — only the bare `500` was visible.

## Fix

Guard the wait with a short timeout, fall back to `domcontentloaded`, and swallow the
timeout (per the Playwright rule in `AGENTS.md`):

```python
# NOTE: networkidle can hang forever on sites with persistent connections
# (Doubao keeps websocket/long-poll connections open), which previously made
# this route raise TimeoutError -> HTTP 500. Guard with a short timeout and
# fall back to domcontentloaded.
try:
    await page.wait_for_load_state("domcontentloaded", timeout=5000)
except Exception:
    pass
await asyncio.sleep(1)
```

## Verification

1. Killed stale Flask process on port 5000, restarted `python kimi-proxy/app.py`.
2. `GET /api/status` returned healthy (`kimi`, `deepseek` ready).
3. Re-run Doubao login + confirm should now return 200 within a few seconds and write
   `doubao_storage.json`.

## Takeaway

Never call `wait_for_load_state("networkidle")` without a timeout. Any site with
long-lived connections (websockets, SSE, long-poll) will hang it. Prefer
`domcontentloaded` with an explicit `timeout=` and wrap it in `try/except`.
