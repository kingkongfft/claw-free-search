#!/usr/bin/env python3
"""
AI Web Chat Proxy Router
========================
Routes chat requests across multiple free AI backends (Google AI, DuckDuckGo AI,
Perplexity) using a round-robin strategy.  All providers are always ready — no
login required.

Architecture
------------
All Playwright operations run inside a single background thread that owns an asyncio
event loop.  Flask handler threads communicate with the browser thread via a
``queue.Queue`` of command dicts; results are returned through per-command
``threading.Event`` + result slots so Flask can block until the browser work completes.
"""

import asyncio
import queue
import threading
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-background-networking",
]

STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});
window.chrome = { runtime: {} };
"""

# ---------------------------------------------------------------------------
# Provider registry — all free, no login needed
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "google": {
        "name": "Google AI",
        "url": "https://www.google.com/search?q={query}&udm=50",
        "color": "#4285f4",
        "icon": "G",
        "type": "search",
    },
    "duckduckgo": {
        "name": "DuckDuckGo AI",
        "url": "https://duck.ai/",
        "color": "#de5833",
        "icon": "D",
        "type": "chat",
    },
    "perplexity": {
        "name": "Perplexity",
        "url": "https://www.perplexity.ai/search?q={query}",
        "color": "#20b2aa",
        "icon": "P",
        "type": "search",
    },
}

# ---------------------------------------------------------------------------
# Round-robin
# ---------------------------------------------------------------------------

_rr_index: int = 0
_rr_lock = threading.Lock()


def _next_provider() -> str | None:
    """Return the key of the next provider (round-robin)."""
    global _rr_index
    with _rr_lock:
        keys = list(PROVIDERS.keys())
        if not keys:
            return None
        key = keys[_rr_index % len(keys)]
        _rr_index += 1
        return key


# ---------------------------------------------------------------------------
# Browser-thread machinery
# ---------------------------------------------------------------------------

_cmd_queue: queue.Queue = queue.Queue()


class _Result:
    """Carrier for a single command's outcome, signalled via a threading.Event."""

    def __init__(self):
        self._event = threading.Event()
        self.value = None
        self.error: Exception | None = None
        self.http_status: int | None = None

    def set_ok(self, value):
        self.value = value
        self._event.set()

    def set_client_error(self, message: str):
        self.value = {"error": message}
        self.http_status = 400
        self._event.set()

    def set_error(self, exc: Exception):
        self.error = exc
        self._event.set()

    def wait(self, timeout: float = 180.0):
        if not self._event.wait(timeout):
            raise TimeoutError("Browser thread did not respond in time")
        if self.error is not None:
            raise self.error
        return self.value


def _send(cmd: str, **payload) -> tuple:
    """Send a command to the browser thread; block until complete."""
    result = _Result()
    _cmd_queue.put({"cmd": cmd, "payload": payload, "result": result})
    value = result.wait()
    return value, result.http_status


# ---------------------------------------------------------------------------
# Browser-thread state
# ---------------------------------------------------------------------------

_chat_bstate: dict = {
    "pw": None,
    "browser": None,
}

# ---------------------------------------------------------------------------
# Browser-thread event loop
# ---------------------------------------------------------------------------


async def _browser_loop():
    loop = asyncio.get_running_loop()
    while True:
        item = await loop.run_in_executor(None, _cmd_queue.get)
        cmd: str = item["cmd"]
        payload: dict = item["payload"]
        result: _Result = item["result"]
        try:
            if cmd == "chat":
                out = await _cmd_chat(payload["message"], payload.get("provider"))
                if "_client_error" in out:
                    result.set_client_error(out["_client_error"])
                else:
                    result.set_ok(out)

            elif cmd == "close":
                await _cmd_close()
                result.set_ok({"success": True})

            elif cmd == "shutdown":
                await _cmd_close()
                result.set_ok(None)
                return

            else:
                result.set_error(ValueError(f"Unknown command: {cmd}"))

        except Exception as exc:
            result.set_error(exc)


# ---------------------------------------------------------------------------
# Chat browser helpers
# ---------------------------------------------------------------------------


async def _ensure_chat_browser():
    """Start the shared headless chat browser once with stealth args."""
    if _chat_bstate["browser"] is not None:
        return
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=STEALTH_ARGS,
    )
    _chat_bstate["pw"] = pw
    _chat_bstate["browser"] = browser


async def _close_chat_browser():
    if _chat_bstate["browser"]:
        try:
            await _chat_bstate["browser"].close()
        except Exception:
            pass
    if _chat_bstate["pw"]:
        try:
            await _chat_bstate["pw"].stop()
        except Exception:
            pass
    _chat_bstate["browser"] = None
    _chat_bstate["pw"] = None


async def _new_page():
    """Create a fresh page with stealth context."""
    await _ensure_chat_browser()
    context = await _chat_bstate["browser"].new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=USER_AGENT,
        locale="en-US",
    )
    await context.add_init_script(STEALTH_INIT_JS)
    page = await context.new_page()
    return context, page


# ---------------------------------------------------------------------------
# Response extractors per provider
# ---------------------------------------------------------------------------


async def _extract_google_response(page) -> str:
    """Extract AI Overview or search result content from Google."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(3)

    # Detect bot / CAPTCHA page early
    try:
        body_text = await page.evaluate("() => document.body.innerText || ''")
        if "unusual traffic" in body_text or "captcha" in body_text.lower():
            return (
                "Google has detected automated traffic from this IP and is "
                "showing a CAPTCHA. This provider may not work reliably from "
                "server environments."
            )
    except Exception:
        pass

    # Try AI Overview selectors
    selectors = [
        "[data-attrid='wa:/description']",
        "[class*='ai-overview']",
        "[class*='AIO']",
        "[data-sncf]",
        ".IZ6rdc",
        "[class*='kp-wholepage']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                text = text.strip()
                if len(text) > 30:
                    return text
        except Exception:
            continue

    # Fallback: grab all visible text from main content
    try:
        body_text = await page.evaluate("""() => {
            const main = document.querySelector('#main') || document.querySelector('#rso') || document.body;
            return main.innerText || '';
        }""")
        lines = [line.strip() for line in body_text.split('\n') if len(line.strip()) > 20]
        if lines:
            return '\n\n'.join(lines[:30])
    except Exception:
        pass

    return "No AI response extracted from Google. The page may have loaded differently."


async def _extract_duckduckgo_response(page, message: str) -> str:
    """Navigate to DuckDuckGo AI, type the message, submit, and extract the response."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    # Wait for the chat input textarea to appear
    textarea = None
    for _ in range(40):
        try:
            textarea = await page.query_selector(
                "textarea#searchbox_input, "
                "textarea[placeholder*='Ask'], "
                "textarea[placeholder*='ask'], "
                "textarea[placeholder*='Type'], "
                "textarea[placeholder*='Message'], "
                "textarea"
            )
            if textarea and await textarea.is_visible():
                break
            textarea = None
        except Exception:
            pass
        await asyncio.sleep(0.5)

    if not textarea:
        return "Could not find DuckDuckGo AI chat input."

    # Type the message
    try:
        await textarea.click()
        await textarea.fill(message)
        await asyncio.sleep(0.5)
    except Exception as e:
        return f"Failed to type message: {e}"

    # Find and click the submit / Ask button
    submit_clicked = False
    # Try clicking various submit buttons
    submit_selectors = [
        "button[aria-label='Ask']",
        "button[aria-label='ask']",
        "button[type='submit']",
        "button.submit",
        "form button",
    ]
    for sel in submit_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                submit_clicked = True
                break
        except Exception:
            continue

    if not submit_clicked:
        # Fallback: press Enter
        await page.keyboard.press("Enter")

    # Wait for the response to appear — DDG streams the response token by token
    # We poll the page, looking for new content that wasn't there before.
    await asyncio.sleep(3)

    # Snapshot baseline text
    try:
        baseline_text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        baseline_text = ""

    # Poll for new substantial content
    response = ""
    for attempt in range(60):
        await asyncio.sleep(1.5)
        try:
            await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            continue

        # Look for new content that appeared after our baseline
        # The AI response should be new text that wasn't in the baseline
        new_text = ""
        # Try to find the response container directly
        response_selectors = [
            "[class*='chat-msg']",
            "[class*='message']",
            "[class*='response']",
            "[class*='answer']",
            "[class*='result']",
            "[class*='prose']",
            "#chat-history",
            "[class*='markdown']",
        ]
        for sel in response_selectors:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    t = (await el.inner_text()).strip()
                    # Must be new content (not in baseline) and substantial
                    if len(t) > 10 and t not in baseline_text:
                        if len(t) > len(new_text):
                            new_text = t
            except Exception:
                continue

        # Also check for any visible element with the user's query echoed
        # followed by response text
        if not new_text:
            try:
                full = await page.evaluate("""() => {
                    const all = document.querySelectorAll('div, p, span, article, section');
                    const texts = [];
                    for (const el of all) {
                        if (el.children.length === 0 || el.querySelector('p, div')) {
                            const t = (el.innerText || '').trim();
                            if (t.length > 20) texts.push(t);
                        }
                    }
                    return texts.join('\\n---\\n');
                }""")
                # Find chunks not in baseline
                chunks = full.split('\n---\n')
                for chunk in chunks:
                    chunk = chunk.strip()
                    if len(chunk) > 30 and chunk not in baseline_text:
                        if len(chunk) > len(new_text):
                            new_text = chunk
            except Exception:
                pass

        if new_text and len(new_text) > 20:
            # Check if it seems stable (content stopped growing)
            response = new_text
            # Give it a few more seconds to check if more is streaming in
            for _ in range(5):
                await asyncio.sleep(1)
                try:
                    await page.evaluate("() => document.body.innerText || ''")
                except Exception:
                    continue
                # Re-check for response in latest
                latest_new = ""
                for sel in response_selectors:
                    try:
                        elements = await page.query_selector_all(sel)
                        for el in elements:
                            t = (await el.inner_text()).strip()
                            if len(t) > 10 and t not in baseline_text:
                                if len(t) > len(latest_new):
                                    latest_new = t
                    except Exception:
                        continue
                if latest_new and len(latest_new) > len(response):
                    response = latest_new
                else:
                    break
            if response:
                return response

    if response:
        return response
    return "No AI response extracted from DuckDuckGo. The response may still be loading."


async def _extract_perplexity_response(page) -> str:
    """Extract answer from Perplexity search results."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(5)

    # Detect Cloudflare challenge
    try:
        body_text = await page.evaluate("() => document.body.innerText || ''")
        if "security verification" in body_text.lower() or "checking" in body_text.lower():
            # Wait longer — Cloudflare challenge may auto-resolve
            await asyncio.sleep(10)
            body_text = await page.evaluate("() => document.body.innerText || ''")
            if "security verification" in body_text.lower():
                return (
                    "Perplexity is showing a Cloudflare security verification page. "
                    "This may resolve automatically if you wait, or it may indicate "
                    "that the IP is being rate-limited."
                )
    except Exception:
        pass

    # Perplexity renders answer in various containers
    selectors = [
        ".prose",
        "[class*='answer']",
        "[class*='markdown']",
        "[class*='prose']",
        "#answer",
        "[data-testid='answer']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                text = text.strip()
                if len(text) > 30:
                    return text
        except Exception:
            continue

    # Fallback
    try:
        body = await page.evaluate("() => document.body.innerText || ''")
        lines = [line.strip() for line in body.split('\n') if len(line.strip()) > 20]
        if lines:
            return '\n\n'.join(lines[:30])
    except Exception:
        pass

    return "No AI response extracted from Perplexity."


async def _cmd_chat(message: str, provider: str | None = None) -> dict:
    """Send a message to a free AI provider, navigate, and extract the response."""
    if provider is None:
        provider = _next_provider()
    if provider is None:
        return {"_client_error": "No providers available."}
    if provider not in PROVIDERS:
        return {"_client_error": f"Unknown provider: {provider}"}

    prov = PROVIDERS[provider]
    query = quote_plus(message)
    url = prov["url"].format(query=query)

    context, page = await _new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        if provider == "google":
            response = await _extract_google_response(page)
        elif provider == "duckduckgo":
            response = await _extract_duckduckgo_response(page, message)
        elif provider == "perplexity":
            response = await _extract_perplexity_response(page)
        else:
            response = f"Provider {provider} not implemented."
    finally:
        try:
            await context.close()
        except Exception:
            pass

    if response:
        return {"success": True, "response": response, "provider": provider}
    return {"_client_error": f"No response from {prov['name']}."}


async def _cmd_close():
    await _close_chat_browser()


# ---------------------------------------------------------------------------
# Browser thread startup
# ---------------------------------------------------------------------------


def _start_browser_thread():
    def _run():
        asyncio.run(_browser_loop())

    t = threading.Thread(target=_run, daemon=True, name="playwright-browser")
    t.start()
    return t


_browser_thread = _start_browser_thread()

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    providers_status = {
        k: {
            "name": v["name"],
            "color": v["color"],
            "icon": v["icon"],
        }
        for k, v in PROVIDERS.items()
    }
    return render_template("index.html", providers=providers_status)


@app.route("/api/providers")
def api_providers():
    return jsonify(
        {
            k: {
                "name": v["name"],
                "color": v["color"],
                "icon": v["icon"],
                "ready": True,
                "active": False,
            }
            for k, v in PROVIDERS.items()
        }
    )


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "providers": {
                k: {"name": v["name"], "ready": True}
                for k, v in PROVIDERS.items()
            },
            "any_ready": True,
            "ready_providers": list(PROVIDERS.keys()),
        }
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.json or {}
    message = body.get("message", "").strip()
    if not message:
        return jsonify({"error": "No message provided"}), 400
    provider = body.get("provider") or None
    try:
        data, status = _send("chat", message=message, provider=provider)
        return jsonify(data), (status or 200)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/close", methods=["POST"])
def api_close():
    try:
        data, _ = _send("close")
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting AI Chat Router (Free Providers)...")
    print("Providers: Google AI, DuckDuckGo AI, Perplexity")
    print("Open http://localhost:5000 in your browser")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
