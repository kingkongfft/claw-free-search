# Chat Router Status — 2026-06-20

## Changes Made

Replaced login-required providers (Kimi, DeepSeek, Doubao) with free providers:

| Old Provider | New Provider | URL |
|---|---|---|
| Kimi | **Google AI** | `google.com/search?q={query}&udm=50` |
| DeepSeek | **DuckDuckGo AI** | `duck.ai/` |
| Doubao | **Perplexity** | `perplexity.ai/search?q={query}` |

### Files Modified
- `chat-router/app.py` — Complete rewrite: removed login/storage/cookies, added free providers with stealth browser launch, per-provider response extractors
- `chat-router/templates/index.html` — Removed login modal, simplified to provider-select UI
- `chat-router/static/style.css` — Removed modal/login CSS
- `AGENTS.md` — Updated to reflect new providers

### Files Created
- `chat-router/test_providers.py` — New smoke test for free providers

## Current Test Results

All 3 providers fail to return actual AI content from headless Chromium:

### Google AI
- **Issue**: Bot detection triggered — "Our systems have detected unusual traffic from your computer network"
- **Cause**: Google blocks headless Chromium from this IP (202.65.196.242)
- **Status**: Unusable in headless mode from this network

### DuckDuckGo AI (duck.ai)
- **Issue**: Extractor grabs sidebar/nav text ("New Chat", "New Voice Chat", etc.) instead of AI response
- **Root cause**: `duck.ai` is a single-page app — navigating to it doesn't auto-send the query. Need to: (1) navigate to duck.ai, (2) type the query in textarea, (3) click "Ask" button, (4) wait for streamed response
- **Status**: Extractor logic needs rewrite (the textarea+submit flow exists in code but the DOM selectors are wrong)

### Perplexity
- **Issue**: Cloudflare security verification page blocks access
- **Cause**: Perplexity uses Cloudflare bot protection, headless Chromium gets challenged
- **Status**: Unusable in headless mode

## Architecture Notes

- Browser launched with stealth args: `--disable-blink-features=AutomationControlled` + init script overriding `navigator.webdriver`
- Each chat request creates a fresh browser context + page (no session reuse)
- Flask routes: `/api/providers`, `/api/status`, `/api/chat`, `/api/close`
- Round-robin or pinned provider selection via `provider` field in chat request body

## Next Steps

1. **DuckDuckGo AI**: Debug the actual DOM structure of `duck.ai` — need to inspect the real page to find correct selectors for textarea, submit button, and response container
2. **Google / Perplexity**: Both blocked by bot detection from this IP. Options:
   - Use a different proxy/VPN with cleaner IP
   - Try non-headless mode (visible browser) — may bypass some detection
   - Consider alternative free providers that are more bot-friendly (e.g., HuggingFace chat, Poe, etc.)
3. **Test script**: `chat-router/test_providers.py` exists but timed out during provider tests — needs working providers to validate
