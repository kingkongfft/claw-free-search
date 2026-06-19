# AGENTS.md — AI Web Chat Proxy Router

Orientation for agentic coding assistants working in this repository.

## Project Overview

Python tools for chatting with free web AI assistants (Google AI, DuckDuckGo AI,
Perplexity) without any API key or login, by driving a real Chromium browser via
Playwright.

1. **Root CLI scripts** (`kimi_search.py`, `kimi_auth.py`) — legacy Kimi-specific
   tools (no longer used by chat-router).
2. **`chat-router/`** — a Flask app (`app.py`) that exposes a multi-provider chat router
   over REST. Requests are load-balanced **round-robin** across all three free providers.
   Single-page vanilla-JS UI in `templates/index.html`.

Core technique: launch headless Chromium per request, navigate to the provider's search/chat
page with the user's query, and extract the AI-generated response from the DOM.

## Repository Layout

```
claw-free-search/
├── kimi_auth.py            # Interactive CLI: KimiAuth + KimiSearch classes
├── kimi_search.py          # Simple CLI: login + search
├── requirements.txt        # Root deps: playwright, requests
├── chat-router/
│   ├── app.py              # Flask router; async Playwright in a dedicated thread
│   ├── test_app.py         # Standalone smoke test (runs against a live server)
│   ├── requirements.txt    # flask, playwright, requests
│   ├── templates/index.html
│   └── static/style.css
```

## Setup

```bash
pip install -r requirements.txt              # root scripts
pip install -r chat-router/requirements.txt   # proxy
playwright install chromium
```

Python 3.10+ assumed (uses `str | None` union syntax, built-in generics).

## Build / Run

```bash
python kimi_search.py --login                # first-time Kimi login (visible browser)
python kimi_search.py "what is quantum computing"
python kimi_auth.py                          # interactive menu

python chat-router/app.py                     # start proxy on http://localhost:5000
```

## Lint / Format

Ruff is used (see `.ruff_cache/`, version 0.12.5). No config file exists yet — defaults apply.

```bash
ruff check .                 # lint
ruff check --fix .           # autofix
ruff format .                # format (black-compatible)
```

When adding a config, prefer `pyproject.toml` with `[tool.ruff]`. Formatter = ruff/black
defaults (88 cols). Keep lines readable (≤ 100 chars).

## Testing

`chat-router/test_app.py` is a **standalone script**, NOT pytest. It drives the live UI/API
with sync Playwright and exits non-zero on failure. The server must be running first.

```bash
python chat-router/app.py &                   # start server on :5000
python chat-router/test_app.py                # run all smoke tests
```

There is no single-test selector; it is one script. To run one check, comment out others
in `run_tests()`. When adding real unit tests:

- Use `pytest`; place files in `tests/test_<module>.py`.
- Single file: `pytest tests/test_kimi_auth.py -v`
- Single test:  `pytest tests/test_kimi_auth.py::test_name -v`
- Browser tests must use `playwright.async_api` + `pytest-asyncio`. NEVER call the sync
  Playwright API from a non-main thread.

## CRITICAL: Playwright Threading

`chat-router/app.py` uses **async Playwright in one dedicated background thread** that owns
an asyncio event loop. Flask handler threads talk to it via a `queue.Queue` of command
dicts; results return through per-command result slots. **Never** call `sync_playwright`
from a Flask request thread or spawn `threading.Thread` running the sync API — it raises
`cannot switch to a different thread`. Add new browser work as a `_cmd_*` coroutine
dispatched through the queue, not inline in a route.

## Code Style

### Imports
Three PEP 8 groups separated by blank lines: stdlib, third-party, local.
```python
import asyncio
import json
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from playwright.async_api import async_playwright
```
Prefer `from pathlib import Path` and `Path` objects over `os.path` and raw strings.

### Naming
| Kind | Convention | Example |
|------|-----------|---------|
| Module | `snake_case` | `app.py` |
| Function / method | `snake_case` | `_cmd_chat()` |
| Internal/browser-thread helpers | `_snake_case` | `_browser_loop()`, `_chat_bstate` |
| Constants / registries | `UPPER_SNAKE_CASE` | `PROVIDERS`, `USER_AGENT` |

### Types
Annotate all function signatures. Use built-in generics (`dict[str, str]`) and `X | None`
unions (3.10+). Avoid `Any` unless unavoidable.
```python
async def _cmd_chat(message: str, provider: str | None = None) -> dict:
    ...
```

### Error Handling
- Never bare `except:` — catch `Exception as e` and print/log before swallowing.
- Bare `except: pass` is acceptable ONLY in browser/cleanup teardown paths.
- Flask routes must ALWAYS return JSON: `jsonify({"error": str(e)}), 500` on failure;
  never let an exception escape a route.

### Playwright
- Long-lived sessions: explicit `.start()` / `.stop()`; standalone scripts: `with` form.
- Prefer `page.wait_for_selector(sel, timeout=N)` over bare `time.sleep()`.
- Guard `wait_for_load_state` with a timeout (e.g. `domcontentloaded`, `timeout=5000`) —
  `networkidle` can hang on sites with persistent connections (e.g. DuckDuckGo).

### Flask
- Routes via `@app.route(path, methods=[...])`; return `jsonify(...)` everywhere.
- Success: `{"success": True, ...}` (HTTP 200). Error: `{"error": "..."}` (4xx/5xx).
- Multi-provider routes use `/api/chat` with optional `provider` field in the body.

### File I/O
- Always `encoding="utf-8"`.
- JSON writes: `json.dump(..., indent=2, ensure_ascii=False)` to preserve CJK/emoji.

### Docs
Module docstring at top of every file; one-line docstrings on public functions; inline
comments for non-obvious selectors and Playwright workarounds.

## Secrets & Sensitive Data
- NEVER commit `*_cookies.json`, `*_auth.json`, `*_storage.json` — all gitignored.
- No API keys or passwords stored; only manually captured session state lives locally.

## Provider Registry
New backends are added to the `PROVIDERS` dict in `chat-router/app.py`. All providers
are always ready — no login, no session files. Each provider has a `url` template with
`{query}` placeholder, a response extractor function, and visual metadata (color, icon).
