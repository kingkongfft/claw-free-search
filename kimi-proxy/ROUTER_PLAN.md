# AI Web Chat Proxy Router — Plan

**Date:** 2026-06-19  
**Status:** Planning

---

## Overview

Upgrade `kimi-proxy` from a single-backend Kimi proxy into a **multi-backend round-robin
router** supporting Kimi, DeepSeek, and Doubao. A single `POST /api/chat` request is
automatically forwarded to whichever backend is next in the rotation (among those that
are logged in and ready). The web UI shows a left-side provider panel that lights up the
active backend in real time.

---

## Target Architecture

```
                    ┌──────────────────────────────────────────┐
Browser ──POST /api/chat──►   Round-Robin Router                │
                    │   ┌──────────┬───────────┬────────────┐  │
                    │   │  Kimi    │ DeepSeek  │   Doubao   │  │
                    │   │ (ready?) │  (ready?) │  (ready?)  │  │
                    │   └────┬─────┴─────┬─────┴──────┬─────┘  │
                    │        │           │             │         │
                    │    headless Chromium per request           │
                    │    (cookies loaded from storage file)      │
                    └──────────────────────────────────────────┘
```

Each provider is **independent**:
- Own login flow (visible browser → save cookies)
- Own storage file on disk
- Own DOM selectors for input field and response extraction
- `ready` = its storage file exists on disk (survives server restarts)

Round-robin only cycles through providers whose storage file exists.

---

## Provider Registry

```python
PROVIDERS = {
    "kimi": {
        "name":    "Kimi",
        "url":     "https://www.kimi.com",
        "storage": Path("kimi_storage.json"),
    },
    "deepseek": {
        "name":    "DeepSeek",
        "url":     "https://chat.deepseek.com/",
        "storage": Path("deepseek_storage.json"),
    },
    "doubao": {
        "name":    "Doubao",
        "url":     "https://www.doubao.com/chat/",
        "storage": Path("doubao_storage.json"),
    },
}
```

Storage files follow the existing unified format:
```json
{"cookies": [...], "localStorage": {...}}
```

All three filenames already match the `*_storage.json` gitignore pattern — no new
`.gitignore` entries needed.

---

## Round-Robin Logic

```python
_rr_index: int = 0   # global, owned by browser thread

def _next_provider() -> str | None:
    """Return the key of the next ready provider, or None if none are ready."""
    global _rr_index
    ready = [k for k in PROVIDERS if PROVIDERS[k]["storage"].exists()]
    if not ready:
        return None
    key = ready[_rr_index % len(ready)]
    _rr_index += 1
    return key
```

- Cycles only among **ready** providers (storage file exists)
- If a provider goes offline mid-rotation it is skipped automatically
- Index wraps around via `% len(ready)` so it never goes out of bounds

---

## Provider-Specific DOM Config

Each site has a different DOM structure. Selectors are kept in a config dict so the
generic `_cmd_chat` function never needs site-specific `if` branches.

```python
PROVIDER_CONFIGS = {
    "kimi": {
        "input_selectors": [
            'div[contenteditable="true"]',
            "textarea",
            '[class*="editor"]',
            '[class*="input"] textarea',
        ],
        "segment_selectors": [
            '[class*="segment"]',
            '[class*="turn--"]',
            '[class*="reply"]',
            '[class*="ai-message"]',
            '[class*="assistant"]',
        ],
        "generating_selector": '[class*="loading"], [class*="generating"], [class*="stop"], .stop-btn, [class*="cursor-blink"]',
        "noise_patterns": [
            r"^Expand Sidebar", r"^Copy$", r"^Share$", r"^Search",
            r"^K2\.", r"^Ask away", r"^Pics work", r"^\d+ results",
        ],
    },
    "deepseek": {
        # To be confirmed by DOM inspection after first login
        "input_selectors": [
            'textarea#chat-input',
            '[class*="chat-input"] textarea',
            'div[contenteditable="true"]',
        ],
        "segment_selectors": [
            '[class*="ds-markdown"]',
            '[class*="message-content"]',
            '[class*="assistant-message"]',
        ],
        "generating_selector": '[class*="loading"], [class*="stop-btn"], [class*="generating"]',
        "noise_patterns": [],
    },
    "doubao": {
        # To be confirmed by DOM inspection after first login
        "input_selectors": [
            '[class*="input-area"] [contenteditable]',
            '[class*="chat-input-area"] textarea',
            'div[contenteditable="true"]',
        ],
        "segment_selectors": [
            '[class*="chat-content"]',
            '[class*="bot-message"]',
            '[class*="assistant"]',
        ],
        "generating_selector": '[class*="loading"], [class*="stop"], [class*="generating"]',
        "noise_patterns": [],
    },
}
```

> **Note:** DeepSeek and Doubao selectors are initial guesses based on common patterns.
> They must be verified by inspecting the live DOM after a real login session. The
> implementation step will use a headless screenshot + `page.content()` dump to confirm
> the actual class names before finalising.

---

## API Changes

### Unchanged endpoints

| Endpoint | Method | Notes |
|----------|--------|-------|
| `GET /` | GET | Now passes all providers' status to template |
| `POST /api/close` | POST | Resets login browser state (unchanged) |

### Modified endpoints

#### `GET /api/status`
Now returns per-provider status instead of a single boolean:
```json
{
  "providers": {
    "kimi":     {"name": "Kimi",     "ready": true,  "active": false},
    "deepseek": {"name": "DeepSeek", "ready": false, "active": false},
    "doubao":   {"name": "Doubao",   "ready": true,  "active": false}
  },
  "any_ready": true
}
```

#### `POST /api/chat`
Request body unchanged: `{"message": "..."}`.  
Response now includes `used_provider`:
```json
{
  "success": true,
  "response": "...",
  "used_provider": "kimi"
}
```
While the request is in flight, the backend sets an `_active_provider` key that the
status endpoint reflects so the frontend can light up the correct icon.

### New endpoints

#### `GET /api/providers`
Returns the same provider status structure as the new `/api/status`. Used by the
frontend's 5-second polling loop to refresh the left panel.

#### `POST /api/login/<provider>`
Opens a visible browser for the given provider.
```
POST /api/login/deepseek
→ {"success": true, "message": "Browser opened. Please login to DeepSeek."}
```
Returns `400` if `provider` is not in `PROVIDERS`.

#### `POST /api/login/<provider>/confirm`
Saves the session for the given provider, closes the visible browser.
```
POST /api/login/deepseek/confirm
→ {"success": true, "logged_in": true, "message": "DeepSeek session saved!"}
```

---

## Frontend UI Changes

### Layout: left sidebar + right chat

```
┌─────────────────────────────────────────────────────────┐
│                    AI Chat Router                        │  ← header
├──────────────────┬──────────────────────────────────────┤
│  Providers       │  Chat                                 │
│                  │                                       │
│  ┌────────────┐  │  ┌──────────────────────────────┐    │
│  │ ● Kimi  ✓ │  │  │  [Kimi] 今天中国足球新闻...  │    │
│  │  [active] │  │  └──────────────────────────────┘    │
│  └────────────┘  │                                       │
│  ┌────────────┐  │  ┌──────────────────────────────┐    │
│  │ ● DeepSeek │  │  │  你   今天深圳天气。          │    │
│  │  [Login]  │  │  └──────────────────────────────┘    │
│  └────────────┘  │                                       │
│  ┌────────────┐  │  [input textarea]         [Send]      │
│  │ ● Doubao  │  │                                       │
│  │  [Login]  │  │                                       │
│  └────────────┘  │                                       │
└──────────────────┴──────────────────────────────────────┘
```

### Provider card states

| State | Visual |
|-------|--------|
| Not logged in | Grey dot, "Login" button shown |
| Ready / logged in | Green dot, "Re-login" button shown |
| Currently serving request | **Pulsing highlight**, spinner icon |
| Login window open | Amber dot, "I've logged in" button shown |

### Per-message provider badge

Each assistant reply bubble shows which provider answered:

```
┌────────────────────────────────────────┐
│ [Kimi]  今天中国足球新闻的最新消息是…  │
│                               [Copy]   │
└────────────────────────────────────────┘
```

### Status polling

```javascript
setInterval(async () => {
    const data = await fetch('/api/providers').then(r => r.json());
    updateProviderPanel(data.providers);
}, 5000);
```

---

## File Change Summary

| File | Change |
|------|--------|
| `kimi-proxy/app.py` | Major refactor: provider registry, per-provider storage, round-robin, new login routes |
| `kimi-proxy/templates/index.html` | Full redesign: left sidebar + provider cards + active highlight + provider badge on messages |
| `kimi-proxy/static/style.css` | Full redesign: two-column layout, provider card styles, active/ready/offline states, pulse animation |
| `kimi-proxy/deepseek_storage.json` | New (created after DeepSeek login, gitignored) |
| `kimi-proxy/doubao_storage.json` | New (created after Doubao login, gitignored) |
| `kimi-proxy/test_app.py` | Update tests for new multi-provider API shape |

---

## Implementation Order

1. **Backend: provider abstraction**
   - Extract Kimi-specific logic into `PROVIDERS` + `PROVIDER_CONFIGS` dicts
   - Make `_cmd_chat` accept a `provider` argument and use config-driven selectors
   - Keep existing behaviour 100% intact for Kimi

2. **Backend: new login routes**
   - Generalise `_cmd_login` / `_cmd_login_confirm` to accept a provider key
   - Add `/api/login/<provider>` and `/api/login/<provider>/confirm`
   - Keep old `/api/login` + `/api/login/confirm` as aliases for `kimi` (backwards compat)

3. **Backend: round-robin + providers status**
   - Implement `_next_provider()` with `_rr_index`
   - Add `_active_provider` to `_bstate` (set during chat, cleared after)
   - Update `/api/status` and add `/api/providers`

4. **DOM selector validation (requires login)**
   - For DeepSeek and Doubao: perform a real login, dump `page.content()` and take a
     screenshot in headless=False mode to confirm actual class names
   - Update `PROVIDER_CONFIGS` with confirmed selectors

5. **Frontend: layout + provider panel**
   - Redesign to two-column layout
   - Provider cards with dynamic state (ready / offline / active / login-pending)
   - Per-message provider badge

6. **Frontend: active provider highlight**
   - On `POST /api/chat` response, read `used_provider` and briefly flash the
     corresponding provider card

7. **Testing**
   - Update `test_app.py` for new API surface
   - Manual end-to-end test with at least one provider logged in

---

## Open Questions

1. **DeepSeek / Doubao selectors** — need real DOM inspection after first login.
   Implementation will take a headless screenshot + HTML dump to confirm.

2. **Round-robin weighting** — currently equal weight. Could add priority (e.g. prefer
   the fastest responder) in a future iteration.

3. **UI language** — currently English. Can switch to Chinese if preferred.

4. **Fallback behaviour** — if the selected provider fails (timeout, session expired),
   should the router automatically retry with the next provider? Current plan: return
   error to user and let them retry. Can be made automatic later.

5. **Concurrent requests** — round-robin index is a single global integer. If two chat
   requests arrive simultaneously, they may hit the same provider. Acceptable for a
   personal proxy; can be addressed with per-request locking if needed.
