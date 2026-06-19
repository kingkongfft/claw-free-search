#!/usr/bin/env python3
"""
AI Web Chat Proxy Router
========================
Routes chat requests across multiple AI backends (Kimi, DeepSeek, Doubao) using a
round-robin strategy.  Only backends that have a saved session file are included in
the rotation.

Architecture
------------
All Playwright operations run inside a single background thread that owns an asyncio
event loop.  Flask handler threads communicate with the browser thread via a
``queue.Queue`` of command dicts; results are returned through per-command
``threading.Event`` + result slots so Flask can block until the browser work completes.
"""

import asyncio
import json
import queue
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "kimi": {
        "name": "Kimi",
        "url": "https://www.kimi.com",
        "storage": Path("kimi_storage.json"),
        "color": "#7c5cbf",
        "icon": "K",
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://chat.deepseek.com/",
        "storage": Path("deepseek_storage.json"),
        "color": "#1a73e8",
        "icon": "D",
    },
    "doubao": {
        "name": "Doubao",
        "url": "https://www.doubao.com/chat/",
        "storage": Path("doubao_storage.json"),
        "color": "#00b96b",
        "icon": "B",
    },
    "google": {
        "name": "Google AI",
        "url": "https://www.google.com/search?udm=50&aep=11",
        "storage": Path("google_storage.json"),
        "color": "#ea4335",
        "icon": "G",
    },
}

# Provider-specific DOM selectors.
# input_selectors  : tried in order to find the chat input element
# segment_selectors: tried in order to find AI reply containers
# generating_sel   : querySelector to detect if generation is still in progress
# noise_patterns   : JS regex literals (as strings) to skip UI-chrome text
PROVIDER_CONFIGS: dict[str, dict] = {
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
        "generating_sel": (
            '[class*="loading"], [class*="generating"], '
            '[class*="stop"], .stop-btn, [class*="cursor-blink"]'
        ),
        "noise_patterns": [
            "/^Expand Sidebar/",
            "/^Copy$/",
            "/^Share$/",
            "/^Search/",
            "/^K2\\./",
            "/^Ask away/",
            "/^Pics work/",
            "/^New Chat/",
            "/^\\d+ results/",
        ],
        # JS to detect login success on the page
        "login_check_js": """() => {
            const text = document.body.innerText || '';
            const hasAvatar = document.querySelectorAll('[class*="avatar"]').length > 0;
            const hasNewChat = text.includes('New Chat') || text.includes('\u65b0\u5bf9\u8bdd');
            return hasAvatar || hasNewChat;
        }""",
    },
    "deepseek": {
        "input_selectors": [
            "textarea#chat-input",
            '[class*="chat-input"] textarea',
            'div[contenteditable="true"]',
            "textarea",
        ],
        "segment_selectors": [
            '[class*="ds-markdown"]',
            '[class*="message-content"]',
            '[class*="assistant-message"]',
            '[class*="markdown"]',
        ],
        "generating_sel": (
            '[class*="loading"], [class*="stop-btn"], '
            '[class*="generating"], [class*="thinking"]'
        ),
        "noise_patterns": [],
        "login_check_js": """() => {
            const text = document.body.innerText || '';
            return text.includes('New chat') || text.includes('\u65b0\u5bf9\u8bdd')
                || document.querySelectorAll('[class*="avatar"]').length > 0
                || document.querySelectorAll('[class*="user-info"]').length > 0;
        }""",
    },
    "doubao": {
        "input_selectors": [
            '[class*="input-area"] [contenteditable]',
            '[class*="chat-input"] textarea',
            'div[contenteditable="true"]',
            "textarea",
        ],
        "segment_selectors": [
            '[class*="chat-content"]',
            '[class*="bot-message"]',
            '[class*="assistant"]',
            '[class*="markdown"]',
        ],
        "generating_sel": (
            '[class*="loading"], [class*="stop"], [class*="generating"]'
        ),
        "noise_patterns": [],
        "login_check_js": """() => {
            const text = document.body.innerText || '';
            return text.includes('\u65b0\u5efa\u5bf9\u8bdd') || text.includes('New Chat')
                || document.querySelectorAll('[class*="avatar"]').length > 0;
        }""",
    },
    "google": {
        # Google AI Mode (udm=50). The search box is the input; the AI answer
        # renders into a results container after submitting.
        "input_selectors": [
            'textarea[name="q"]',
            'textarea[aria-label*="Search"]',
            'div[contenteditable="true"][role="combobox"]',
            'div[contenteditable="true"]',
            "textarea",
        ],
        "segment_selectors": [
            '[data-rl][role="presentation"]',
            'div[data-async-context] [class*="markdown"]',
            "[jsname][data-mid]",
            '[class*="markdown"]',
            "[data-attrid]",
        ],
        "generating_sel": (
            '[role="progressbar"], [class*="loading"], '
            '[aria-busy="true"], [class*="thinking"]'
        ),
        "noise_patterns": [
            "/^Search$/",
            "/^Images$/",
            "/^Videos$/",
            "/^News$/",
            "/^Maps$/",
            "/^Shopping$/",
            "/^All$/",
            "/^Sign in$/",
            "/^AI Mode/",
            "/^About \\d+ results/",
        ],
        # Google search works without sign-in; AI Mode availability can depend on
        # account/region. We just confirm the search UI loaded.
        "login_check_js": """() => {
            return document.querySelector('textarea[name="q"]') !== null
                || document.querySelector('div[contenteditable="true"][role="combobox"]') !== null
                || (document.body.innerText || '').length > 0;
        }""",
    },
}

# ---------------------------------------------------------------------------
# Storage helpers  (Flask-thread safe — file I/O only)
# ---------------------------------------------------------------------------


def load_storage(provider: str) -> dict:
    """Load persisted cookies + localStorage for a provider.

    Returns ``{"cookies": [...], "localStorage": {...}}``.
    Handles both the current unified format and the legacy localStorage-only format.
    """
    path = PROVIDERS[provider]["storage"]
    if not path.exists():
        return {"cookies": [], "localStorage": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "cookies" in data:
        return data
    return {"cookies": [], "localStorage": data.get("localStorage", {})}


def save_storage(provider: str, cookies: list, local_storage: dict) -> None:
    """Persist cookies + localStorage for a provider."""
    path = PROVIDERS[provider]["storage"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"cookies": cookies, "localStorage": local_storage},
            f,
            indent=2,
            ensure_ascii=False,
        )


def provider_ready(provider: str) -> bool:
    return PROVIDERS[provider]["storage"].exists()


def ready_providers() -> list[str]:
    return [k for k in PROVIDERS if provider_ready(k)]


# ---------------------------------------------------------------------------
# Round-robin
# ---------------------------------------------------------------------------

_rr_index: int = 0
_rr_lock = threading.Lock()


def _next_provider() -> str | None:
    """Return the key of the next ready provider (round-robin), or None."""
    global _rr_index
    with _rr_lock:
        rp = ready_providers()
        if not rp:
            return None
        key = rp[_rr_index % len(rp)]
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

    def wait(self, timeout: float = 120.0):
        if not self._event.wait(timeout):
            raise TimeoutError("Browser thread did not respond in time")
        if self.error is not None:
            raise self.error
        return self.value


def _send(cmd: str, **payload) -> tuple:
    """Send a command to the browser thread; block until complete.

    Returns ``(value, http_status)``.
    """
    result = _Result()
    _cmd_queue.put({"cmd": cmd, "payload": payload, "result": result})
    value = result.wait()
    return value, result.http_status


# ---------------------------------------------------------------------------
# Browser-thread state
# ---------------------------------------------------------------------------

# Holds the visible login browser (one at a time).
_bstate: dict = {
    "pw": None,
    "browser": None,
    "context": None,
    "page": None,
    "login_provider": None,  # which provider's login window is open
    "login_window_open": False,
    "active_provider": None,  # provider currently serving a chat request
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
            if cmd == "login":
                await _cmd_login(payload["provider"])
                p = payload["provider"]
                result.set_ok(
                    {
                        "success": True,
                        "message": f"Browser opened. Please login to {PROVIDERS[p]['name']}.",
                    }
                )

            elif cmd == "login_confirm":
                out = await _cmd_login_confirm(payload["provider"])
                if "_client_error" in out:
                    result.set_client_error(out["_client_error"])
                else:
                    result.set_ok(out)

            elif cmd == "chat":
                out = await _cmd_chat(payload["message"], payload.get("provider"))
                if "_client_error" in out:
                    result.set_client_error(out["_client_error"])
                else:
                    result.set_ok(out)

            elif cmd == "close":
                await _cmd_close()
                result.set_ok({"success": True})

            elif cmd == "providers":
                result.set_ok(_cmd_providers())

            elif cmd == "shutdown":
                await _cmd_close()
                result.set_ok(None)
                return

            else:
                result.set_error(ValueError(f"Unknown command: {cmd}"))

        except Exception as exc:
            result.set_error(exc)


# ---------------------------------------------------------------------------
# Individual command implementations
# ---------------------------------------------------------------------------


async def _reset_browser():
    for key in ("page", "context", "browser", "pw"):
        obj = _bstate.get(key)
        if obj:
            try:
                if key == "pw":
                    await obj.stop()
                else:
                    await obj.close()
            except Exception:
                pass
    _bstate.update(
        {
            "pw": None,
            "browser": None,
            "context": None,
            "page": None,
            "login_provider": None,
            "login_window_open": False,
        }
    )


async def _cmd_login(provider: str):
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    await _reset_browser()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=USER_AGENT,
    )
    page = await context.new_page()
    await page.goto(PROVIDERS[provider]["url"])
    _bstate.update(
        {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "login_provider": provider,
            "login_window_open": True,
        }
    )


async def _cmd_login_confirm(provider: str) -> dict:
    if not _bstate["page"]:
        return {"_client_error": "No browser open"}
    if _bstate["login_provider"] != provider:
        return {
            "_client_error": f"Login browser is open for '{_bstate['login_provider']}', not '{provider}'"
        }

    page = _bstate["page"]
    context = _bstate["context"]

    # NOTE: networkidle can hang forever on sites with persistent connections
    # (Doubao keeps websocket/long-poll connections open), which previously made
    # this route raise TimeoutError -> HTTP 500. Guard with a short timeout and
    # fall back to domcontentloaded.
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1)

    # Save cookies + localStorage
    try:
        cookies = await context.cookies()
        local_storage = await page.evaluate("""() => {
            const local = {};
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                local[key] = localStorage.getItem(key);
            }
            return local;
        }""")
        save_storage(provider, cookies, local_storage)
    except Exception as e:
        print(f"Failed to save storage for {provider}: {e}")

    # Check login success using provider-specific JS
    login_check_js = PROVIDER_CONFIGS[provider].get("login_check_js", "() => true")
    try:
        is_logged_in: bool = await page.evaluate(login_check_js)
    except Exception:
        is_logged_in = True  # assume logged in if check fails

    _bstate["login_window_open"] = False

    # Close the visible browser
    await _reset_browser()

    name = PROVIDERS[provider]["name"]
    return {
        "success": True,
        "logged_in": is_logged_in,
        "provider": provider,
        "message": f"{name} session saved! You can close the browser window.",
    }


async def _cmd_chat(message: str, provider: str | None = None) -> dict:
    """Send a message via a fresh headless browser for the given provider.

    If provider is None, the round-robin selects the next ready one.
    """
    if provider is None:
        provider = _next_provider()
    if provider is None:
        return {
            "_client_error": "No providers are logged in. Please login to at least one."
        }
    if not provider_ready(provider):
        return {"_client_error": f"{PROVIDERS[provider]['name']} is not logged in."}

    _bstate["active_provider"] = provider
    cfg = PROVIDER_CONFIGS[provider]
    prov = PROVIDERS[provider]

    storage = load_storage(provider)
    cookies: list = storage.get("cookies", [])
    local_storage: dict = storage.get("localStorage", {})

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=USER_AGENT,
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto(prov["url"])
        await page.wait_for_load_state("networkidle")

        if local_storage:
            await page.evaluate(
                """(items) => {
                    for (const [k, v] of Object.entries(items)) {
                        localStorage.setItem(k, v);
                    }
                }""",
                local_storage,
            )
            await page.reload()
            await page.wait_for_load_state("networkidle")

        await asyncio.sleep(2)

        # Find input field
        input_field = None
        for selector in cfg["input_selectors"]:
            try:
                el = await page.wait_for_selector(selector, timeout=5000)
                if el and await el.is_visible():
                    input_field = el
                    break
            except Exception:
                continue

        if not input_field:
            raise RuntimeError(
                f"Could not find chat input on {prov['name']} — "
                "session may have expired, please re-login."
            )

        # Type and send
        await input_field.evaluate("el => el.focus()")
        await asyncio.sleep(0.2)
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.1)
        await page.keyboard.type(message, delay=20)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # Build the JS extractor from provider config
        segment_sel_js = json.dumps(cfg["segment_selectors"])
        noise_patterns_js = (
            ", ".join(cfg["noise_patterns"]) if cfg["noise_patterns"] else ""
        )
        generating_sel = cfg["generating_sel"]

        extractor_js = f"""() => {{
            const segmentSelectors = {segment_sel_js};
            const noisePatterns = [{noise_patterns_js}];
            function isNoise(text) {{
                return noisePatterns.some(p => p.test(text.trim()));
            }}
            let candidates = [];
            for (const sel of segmentSelectors) {{
                document.querySelectorAll(sel).forEach(el => {{
                    const t = (el.innerText || '').trim();
                    if (t.length > 30 && !isNoise(t)) candidates.push(t);
                }});
            }}
            if (candidates.length === 0) {{
                document.querySelectorAll('[class*="markdown"], [class*="prose"]').forEach(el => {{
                    const t = (el.innerText || '').trim();
                    if (t.length > 30 && !isNoise(t)) candidates.push(t);
                }});
            }}
            if (candidates.length === 0) return {{ text: '', generating: false }};
            const best = candidates[candidates.length - 1];
            const generating = document.querySelectorAll('{generating_sel}').length > 0;
            return {{ text: best, generating }};
        }}"""

        # Poll for response
        response = ""
        start = time.time()
        last_text = ""

        while time.time() - start < 90:
            try:
                result = await page.evaluate(extractor_js)
                text = result.get("text", "")
                generating = result.get("generating", False)
                if text and text != message and len(text) > 20:
                    if not generating and text == last_text:
                        response = text
                        break
                    last_text = text
            except Exception as exc:
                print(f"Poll error ({provider}): {exc}")
            await asyncio.sleep(2)

        # Persist updated session
        try:
            updated_cookies = await context.cookies()
            updated_local: dict = await page.evaluate("""() => {
                const local = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    local[key] = localStorage.getItem(key);
                }
                return local;
            }""")
            save_storage(provider, updated_cookies, updated_local)
        except Exception as e:
            print(f"Failed to update storage for {provider}: {e}")

    finally:
        _bstate["active_provider"] = None
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass

    if response:
        return {"success": True, "response": response, "provider": provider}
    raise RuntimeError(
        f"No response from {prov['name']}. Session may have expired — please re-login."
    )


async def _cmd_close():
    await _reset_browser()


def _cmd_providers() -> dict:
    """Return status of all providers (sync — no Playwright I/O)."""
    active = _bstate.get("active_provider")
    login_provider = _bstate.get("login_provider")
    login_open = _bstate.get("login_window_open", False)
    result = {}
    for key, prov in PROVIDERS.items():
        result[key] = {
            "name": prov["name"],
            "color": prov["color"],
            "icon": prov["icon"],
            "ready": provider_ready(key),
            "active": key == active,
            "login_open": login_open and login_provider == key,
        }
    return result


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
            "ready": provider_ready(k),
        }
        for k, v in PROVIDERS.items()
    }
    return render_template("index.html", providers=providers_status)


@app.route("/api/providers")
def api_providers():
    try:
        data, _ = _send("providers")
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/status")
def api_status():
    try:
        data, _ = _send("providers")
        rp = [k for k, v in data.items() if v["ready"]]
        return jsonify(
            {
                "providers": data,
                "any_ready": len(rp) > 0,
                "ready_providers": rp,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/login/<provider>", methods=["POST"])
def api_login(provider: str):
    if provider not in PROVIDERS:
        return jsonify({"error": f"Unknown provider: {provider}"}), 400
    try:
        data, _ = _send("login", provider=provider)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/login/<provider>/confirm", methods=["POST"])
def api_login_confirm(provider: str):
    if provider not in PROVIDERS:
        return jsonify({"error": f"Unknown provider: {provider}"}), 400
    try:
        data, status = _send("login_confirm", provider=provider)
        return jsonify(data), (status or 200)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Backwards-compatible aliases for Kimi
@app.route("/api/login", methods=["POST"])
def api_login_kimi():
    return api_login("kimi")


@app.route("/api/login/confirm", methods=["POST"])
def api_login_confirm_kimi():
    return api_login_confirm("kimi")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.json or {}
    message = body.get("message", "").strip()
    if not message:
        return jsonify({"error": "No message provided"}), 400
    provider = body.get("provider") or None  # optional: pin to a specific provider
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
    print("Starting AI Chat Router...")
    print("Open http://localhost:5000 in your browser")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
