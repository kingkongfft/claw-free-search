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
            '[class*="segment-assistant"]',
            '[class*="paragraph"]',
            '[class*="markdown"]',
            '[class*="segment-content"]',
        ],
        "generating_sel": (
            '[class*="cursor-blink"], [class*="generating"], '
            'button[class*="stop"], [class*="streaming"]'
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

# Long-lived headless browser used to serve chat requests. The browser and its
# Playwright driver are started once (lazily) and reused; each provider gets its
# own cached context + page so navigation/login-state injection only happens on
# first use instead of on every request.
_chat_bstate: dict = {
    "pw": None,
    "browser": None,
    "pages": {},  # provider -> {"context": ctx, "page": page}
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


# ---------------------------------------------------------------------------
# Long-lived chat browser (perf: reused across requests, one page per provider)
# ---------------------------------------------------------------------------


async def _ensure_chat_browser():
    """Start the shared headless chat browser once; reuse afterwards."""
    if _chat_bstate["browser"] is not None:
        return
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    _chat_bstate["pw"] = pw
    _chat_bstate["browser"] = browser
    _chat_bstate["pages"] = {}


async def _get_chat_page(provider: str):
    """Return a ready, logged-in page for *provider*, creating it on first use.

    The page is cached and reused on subsequent requests so we skip the
    cold-start cost (launch + navigate + login-state injection) every time.
    """
    await _ensure_chat_browser()

    cached = _chat_bstate["pages"].get(provider)
    if cached is not None:
        page = cached["page"]
        # Cheap liveness check; if the page died, drop it and recreate below.
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass
        await _invalidate_chat_page(provider)

    cfg = PROVIDER_CONFIGS[provider]
    prov = PROVIDERS[provider]
    storage = load_storage(provider)
    cookies: list = storage.get("cookies", [])
    local_storage: dict = storage.get("localStorage", {})

    context = await _chat_bstate["browser"].new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=USER_AGENT,
    )
    if cookies:
        await context.add_cookies(cookies)

    # (E) Inject localStorage *before* any navigation via an init script so we
    # don't need the extra "navigate -> set -> reload" round-trip.
    if local_storage:
        await context.add_init_script(
            "(() => { const items = "
            + json.dumps(local_storage)
            + "; try { for (const k in items) localStorage.setItem(k, items[k]); }"
            + " catch (e) {} })();"
        )

    page = await context.new_page()
    # (C) domcontentloaded instead of networkidle (hangs on long-poll sites).
    try:
        await page.goto(prov["url"], wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # (C) Wait for the actual input element instead of a blind sleep.
    await _find_chat_input(page, cfg)

    _chat_bstate["pages"][provider] = {"context": context, "page": page}
    return page


async def _invalidate_chat_page(provider: str):
    """Drop a provider's cached page/context (e.g. after re-login)."""
    cached = _chat_bstate["pages"].pop(provider, None)
    if not cached:
        return
    try:
        await cached["context"].close()
    except Exception:
        pass


async def _close_chat_browser():
    for cached in list(_chat_bstate["pages"].values()):
        try:
            await cached["context"].close()
        except Exception:
            pass
    _chat_bstate["pages"] = {}
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


async def _find_chat_input(page, cfg: dict):
    """Locate a visible chat input element, trying configured selectors."""
    for selector in cfg["input_selectors"]:
        try:
            el = await page.wait_for_selector(selector, timeout=8000)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    return None


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

    # A fresh session was just saved — drop any cached chat page so the next
    # request rebuilds it with the new cookies/localStorage.
    await _invalidate_chat_page(provider)

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
    """Send a message via the long-lived headless browser for *provider*.

    Reuses a cached, already-logged-in page (perf), fills the input in one shot,
    and detects completion via an injected MutationObserver instead of polling
    the whole DOM every couple of seconds.
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

    try:
        page = await _get_chat_page(provider)

        input_field = await _find_chat_input(page, cfg)
        if not input_field:
            # Cached page may be stale; rebuild once and retry.
            await _invalidate_chat_page(provider)
            page = await _get_chat_page(provider)
            input_field = await _find_chat_input(page, cfg)
        if not input_field:
            raise RuntimeError(
                f"Could not find chat input on {prov['name']} — "
                "session may have expired, please re-login."
            )

        # (B) Install the streaming observer BEFORE sending, capturing the
        # current best-candidate text so we can tell when a *new* reply appears.
        segment_sel_js = json.dumps(cfg["segment_selectors"])
        noise_patterns_js = (
            ", ".join(cfg["noise_patterns"]) if cfg["noise_patterns"] else ""
        )
        generating_sel = json.dumps(cfg["generating_sel"])

        observer_js = f"""(userMsg) => {{
            const segmentSelectors = {segment_sel_js};
            const noisePatterns = [{noise_patterns_js}];
            function isNoise(t) {{ return noisePatterns.some(p => p.test(t.trim())); }}
            function candidates() {{
                let out = [];
                for (const sel of segmentSelectors) {{
                    document.querySelectorAll(sel).forEach(el => {{
                        const t = (el.innerText || '').trim();
                        // drop noise and the echoed user prompt itself
                        if (t.length > 10 && !isNoise(t) && t !== userMsg) out.push(t);
                    }});
                }}
                if (out.length === 0) {{
                    document.querySelectorAll('[class*="markdown"], [class*="prose"]').forEach(el => {{
                        const t = (el.innerText || '').trim();
                        if (t.length > 10 && !isNoise(t) && t !== userMsg) out.push(t);
                    }});
                }}
                return out;
            }}
            // Snapshot text that already exists before sending (welcome blurbs,
            // prior turns) so we only surface a NEW reply.
            const baseline = new Set(candidates());
            const st = {{ text: '', changed: Date.now() }};
            window.__chatState = st;
            window.__chatPick = () => {{
                const cs = candidates();
                for (let i = cs.length - 1; i >= 0; i--) {{
                    if (!baseline.has(cs[i])) return cs[i];
                }}
                return '';
            }};
            if (window.__chatObserver) window.__chatObserver.disconnect();
            const update = () => {{
                const pick = window.__chatPick();
                if (pick && pick !== st.text) {{ st.text = pick; st.changed = Date.now(); }}
            }};
            const obs = new MutationObserver(update);
            obs.observe(document.body, {{ childList: true, subtree: true, characterData: true }});
            window.__chatObserver = obs;
            return baseline.size;
        }}"""
        await page.evaluate(observer_js, message)

        # Send the message.
        await _fill_input(page, input_field, message)
        await page.keyboard.press("Enter")

        # (B) Read state at a short interval (also re-pick here in case the
        # observer missed a mutation) and finish when the reply text is stable.
        read_state_js = f"""() => {{
            const st = window.__chatState || {{ text: '', changed: 0 }};
            if (window.__chatPick) {{
                const pick = window.__chatPick();
                if (pick && pick !== st.text) {{ st.text = pick; st.changed = Date.now(); }}
            }}
            const generating = document.querySelectorAll({generating_sel}).length > 0;
            return {{ text: st.text, idleMs: Date.now() - st.changed, generating }};
        }}"""

        response = ""
        # Finish when the reply text has been stable (no growth) long enough.
        # If a generating indicator is still present we require a longer quiet
        # window; otherwise a short one. This stays robust even when a
        # provider's "generating" selector matches some always-present element.
        quiet_idle_ms = 900  # stable window when generation looks done
        quiet_busy_ms = 2500  # stable window while still "generating"
        start = time.time()
        got_first = False
        while time.time() - start < 90:
            try:
                state = await page.evaluate(read_state_js)
            except Exception as exc:
                print(f"Read error ({provider}): {exc}")
                await asyncio.sleep(0.5)
                continue
            text = state.get("text", "")
            idle_ms = state.get("idleMs", 0)
            generating = state.get("generating", False)
            if text and len(text) > 10:
                got_first = True
                threshold = quiet_busy_ms if generating else quiet_idle_ms
                if idle_ms >= threshold:
                    response = text
                    break
            # Poll fast while streaming; back off slightly before first token.
            await asyncio.sleep(0.3 if got_first else 0.5)

        # Persist refreshed session (cookies can rotate); keep the page open.
        try:
            cached = _chat_bstate["pages"].get(provider)
            if cached:
                updated_cookies = await cached["context"].cookies()
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

    if response:
        return {"success": True, "response": response, "provider": provider}
    raise RuntimeError(
        f"No response from {prov['name']}. Session may have expired — please re-login."
    )


async def _fill_input(page, input_field, message: str):
    """Set the chat input value reliably, including CJK text.

    Playwright's ``fill`` uses insertText and works for both textareas and
    contenteditable editors, and unlike per-key ``type`` it inputs CJK
    characters correctly in headless Chromium (``type`` can turn them into
    "?"). We fall back to ``insert_text`` then ``type`` only if fill fails.
    """
    try:
        await input_field.click()
    except Exception:
        pass
    try:
        await input_field.fill("")
        await input_field.fill(message)
        return
    except Exception:
        pass
    # Fallbacks for editors that reject fill().
    try:
        await input_field.evaluate("el => el.focus()")
        await page.keyboard.insert_text(message)
        return
    except Exception:
        pass
    await page.keyboard.type(message)


async def _cmd_close():
    await _reset_browser()
    await _close_chat_browser()


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
