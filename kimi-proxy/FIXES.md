# Kimi Proxy — Fix Log

## Fix 1: Playwright Threading Issue (app.py rewrite)

**Date:** 2026-06-19  
**Status:** Resolved

### Problem

`kimi-proxy/app.py` used `playwright.sync_api`, which cannot be called from multiple
threads. Flask serves each request in a new worker thread, so the browser object created
in `POST /api/login` was unreachable from subsequent `POST /api/login/confirm` and
`POST /api/chat` requests, causing:

```
Error: cannot switch to a different thread (which happens to have exited)
```

### Root Cause

`playwright.sync_api` objects are bound to the OS thread that created them. When
`sync_playwright().start()` was called inside a Flask route handler, the resulting
browser/page objects could not be used from any other thread — including the next Flask
request's thread.

### Fix: Option A — Async Playwright + Dedicated Browser Thread + Queue

`app.py` was rewritten with the following architecture:

```
Flask thread  -->  _cmd_queue (Queue)  -->  browser thread (asyncio event loop)
                                                |
                                           playwright.async_api calls
                                                |
              <--  _Result.wait()         <--  result.set_ok() / set_client_error()
```

**Key components:**

| Component | Description |
|-----------|-------------|
| `_browser_loop()` | `async` function that owns all Playwright objects; runs forever inside a single daemon thread via `asyncio.run()` |
| `_cmd_queue` | `queue.Queue` — Flask threads push command dicts here |
| `_Result` | Carries command outcome (value + optional HTTP status); backed by `threading.Event` so Flask threads block until the browser thread responds |
| `_send(cmd, **payload)` | Helper called from Flask routes; puts a command on the queue and blocks on `_Result.wait()` |
| `_start_browser_thread()` | Called once at module load; starts the daemon thread |

**4xx error propagation:**  
Client errors (browser not open, not logged in) are signalled by returning
`{"_client_error": "<message>"}` from the async command functions. The browser loop
detects the key and calls `result.set_client_error(message)`, which sets
`result.http_status = 400`. Flask routes then return `jsonify(data), (status or 200)`.

This avoids `isinstance` checks across threads, which were unreliable due to
module-identity issues when processes are forked or reloaded by Werkzeug.

### Files Changed

- `kimi-proxy/app.py` — full rewrite (sync → async Playwright, queue-based dispatch)
- `kimi-proxy/STATUS.md` — updated to reflect resolved status

---

## Fix 2: Stale Server Processes on Port 5000

**Date:** 2026-06-19  
**Status:** Resolved (operational procedure)

### Problem

During testing, 7 leftover Flask server processes from previous runs were all listening
on port 5000. Test requests were silently routed to old-code instances, causing tests to
report incorrect failures even though the new code was correct.

### Detection

```
netstat -ano | grep :5000
```
Showed 7 PIDs all bound to port 5000.

### Fix

Kill all stale processes before starting a new server:

```bash
# Windows
for pid in <pid1> <pid2> ...; do taskkill //F //PID $pid; done

# Or kill all Python processes (if safe to do so)
taskkill //F //IM python.exe
```

### Prevention

Always use `with_server.py` (from the webapp-testing skill) to manage the server
lifecycle — it reliably terminates the server process on exit:

```bash
python scripts/with_server.py --server "python kimi-proxy/app.py" --port 5000 \
  -- python kimi-proxy/test_app.py
```

---

## Fix 3: Browser Must Stay Open (headless chat rewrite)

**Date:** 2026-06-19  
**Status:** Resolved

### Problem

After the user closed the visible Kimi login window, any subsequent chat request failed:

```
Error: Keyboard.press: Target page, context or browser has been closed
```

The architecture kept a single persistent Playwright browser object in `_bstate`. Once
the user closed that window (or it crashed), all future operations on the stale
`page`/`browser` objects raised immediately.

### Root Cause

Chat was implemented as stateful operations on a long-lived browser page. Closing the
window invalidated the Playwright handle, with no recovery path.

### Fix: Per-request headless browser + persistent cookie storage

**Login flow (unchanged UX):**
1. Visible browser opens → user logs in manually
2. On confirm: cookies + localStorage saved to `kimi_storage.json`
3. Visible browser is **automatically closed** — user never needs to keep it open

**Chat flow (new):**
- Each `POST /api/chat` spins up a **fresh headless Chromium** instance
- Loads `kimi_storage.json` (cookies restored via `context.add_cookies()`,
  localStorage restored via `page.evaluate()` after first navigation)
- Sends the message, polls for reply, saves updated cookies back to disk
- Headless browser is **closed after every request** — no persistent state

```python
pw = await async_playwright().start()
browser = await pw.chromium.launch(headless=True)   # invisible
context = await browser.new_context(...)
await context.add_cookies(cookies)                   # restore session
page = await context.new_page()
await page.goto(KIMI_URL)
# ... restore localStorage, type message, poll reply ...
await browser.close()   # always cleaned up
await pw.stop()
```

**Status endpoint** now returns `browser_ready: true` whenever `kimi_storage.json`
exists on disk — so a server restart automatically recovers without re-login.

### Files Changed

- `kimi-proxy/app.py`
  - `_cmd_login_confirm`: closes visible browser after saving session
  - `_cmd_chat`: full rewrite to headless per-request pattern
  - `_save_current_storage`: unified format `{"cookies": [...], "localStorage": {...}}`
  - `load_storage`: handles both old and new storage formats
  - `_cmd_status`: returns ready if storage file exists (survives restarts)

---

## Fix 4: Wrong content returned / response truncated

**Date:** 2026-06-19  
**Status:** Resolved

### Problem

Responses showed UI chrome text instead of the actual Kimi answer:

```
Expand Sidebar 今天中国足球新闻 今天中国足球新闻 Copy Share Search
中国足球新闻 2026年6月19日 国足最新消息 2026年6月 31 results Ask
away. Pics work too. K2.6 Instant
```

Real answer content was missing entirely.

### Root Cause

The polling JS used broad selectors (`[class*="message"]`, `[class*="content"]`,
`[class*="response"]`) that matched every element on the page. The "longest text wins"
heuristic then selected a large UI-chrome container (sidebar + toolbar) that happened to
concatenate more characters than the actual answer block.

### Fix: Targeted selectors + noise filter

**Backend (`_cmd_chat` poll loop):**

1. **Segment-first selectors** — try Kimi's actual conversation-turn wrappers first:
   `[class*="segment"]`, `[class*="turn--"]`, `[class*="reply"]`, `[class*="ai-message"]`
2. **Last-element strategy** — take the *last* matching element (most recent AI turn),
   not the longest — so new replies are always preferred over accumulated history
3. **Noise filter** — skip any element whose text matches known UI chrome patterns:
   `Expand Sidebar`, `Copy`, `Share`, `Search`, `K2.`, `Ask away`, `Pics work`, `\d+ results`
4. **Markdown fallback** — if no segment found, fall back to `[class*="markdown"]` /
   `[class*="prose"]` blocks

**Frontend (`index.html` + `style.css`):**

- Assistant messages rendered in `<pre class="message-text">` with
  `white-space: pre-wrap` — preserves newlines and indentation from scraped text
- **Copy button** added to each assistant bubble (bottom-right corner)

### Files Changed

- `kimi-proxy/app.py` — `_cmd_chat`: replaced broad-selector + longest-text with
  segment-targeted + last-element + noise-filtered extraction
- `kimi-proxy/templates/index.html` — `addMessage()`: `<pre>` rendering + copy button
- `kimi-proxy/static/style.css` — `.message.assistant`, `pre.message-text`, `.copy-btn`

---

## Test Results (post-fix)

Run with:
```bash
python scripts/with_server.py --server "python kimi-proxy/app.py" --port 5000 \
  -- python kimi-proxy/test_app.py
```

```
Results: 22/22 passed, 0 failed
```

| Test | Result |
|------|--------|
| Page title is 'Kimi Proxy' | PASS |
| H1 heading present | PASS |
| 'Open Kimi Login' button visible | PASS |
| 'I've logged in' button exists | PASS |
| Login section visible on first load | PASS |
| Chat section hidden on first load | PASS |
| GET /api/status → 200 | PASS |
| browser_ready: false before login | PASS |
| storage_exists key present | PASS |
| POST /api/login/confirm (no browser) → 400 | PASS |
| Error message returned for confirm | PASS |
| POST /api/chat (not logged in) → 400 | PASS |
| Error message returned for chat | PASS |
| POST /api/chat (empty message) → 400 | PASS |
| Error says 'No message provided' | PASS |
| POST /api/close (idempotent) → 200 | PASS |
| success: true on close | PASS |
| Status indicator shows 'Not Logged In' | PASS |
| POST /api/login → 200 | PASS |
| success: true on login | PASS |
| Cleanup after login test | PASS |
| (total) | **22/22** |
