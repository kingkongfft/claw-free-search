#!/usr/bin/env python3
"""
Kimi Proxy Web App
Uses Playwright to automate Kimi chat directly.
"""

import json
import time
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

KIMI_URL = "https://www.kimi.com"
STORAGE_FILE = Path("kimi_storage.json")

# Global browser state (thread-safe via GIL for simple assignments)
browser_state = {
    "pw": None,
    "browser": None,
    "context": None,
    "page": None,
    "ready": False,
    "login_window_open": False
}


def load_storage():
    if STORAGE_FILE.exists():
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_storage(storage):
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(storage, f, indent=2, ensure_ascii=False)


def save_current_storage():
    if not browser_state["ready"] or not browser_state["page"]:
        return
    try:
        storage = browser_state["page"].evaluate("""() => {
            const local = {};
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                local[key] = localStorage.getItem(key);
            }
            const session = {};
            for (let i = 0; i < sessionStorage.length; i++) {
                const key = sessionStorage.key(i);
                session[key] = sessionStorage.getItem(key);
            }
            return { localStorage: local, sessionStorage: session };
        }""")
        save_storage(storage)
    except Exception as e:
        print(f"Failed to save storage: {e}")


@app.route("/")
def index():
    is_logged_in = STORAGE_FILE.exists()
    return render_template("index.html", is_logged_in=is_logged_in)


@app.route("/api/status")
def status():
    return jsonify({
        "browser_ready": browser_state["ready"],
        "storage_exists": STORAGE_FILE.exists(),
        "current_url": browser_state["page"].url if browser_state["ready"] and browser_state["page"] else None
    })


@app.route("/api/login", methods=["POST"])
def login():
    """Open Kimi in a new browser window for manual login"""
    global browser_state

    # Close existing browser if any
    if browser_state["page"]:
        try:
            save_current_storage()
            browser_state["page"].close()
        except:
            pass
    if browser_state["context"]:
        try:
            browser_state["context"].close()
        except:
            pass
    if browser_state["browser"]:
        try:
            browser_state["browser"].close()
        except:
            pass
    if browser_state["pw"]:
        try:
            browser_state["pw"].stop()
        except:
            pass

    browser_state = {
        "pw": None, "browser": None, "context": None,
        "page": None, "ready": False, "login_window_open": False
    }

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto(KIMI_URL)

        browser_state = {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "ready": False,
            "login_window_open": True
        }

        return jsonify({
            "success": True,
            "message": "Browser opened. Please login to Kimi."
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/login/confirm", methods=["POST"])
def login_confirm():
    """Confirm login and save session"""
    if not browser_state["page"]:
        return jsonify({"error": "No browser open"}), 400

    try:
        page = browser_state["page"]

        # Wait a bit for page to be ready
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        # Save localStorage and sessionStorage
        save_current_storage()

        # Check if logged in
        is_logged_in = page.evaluate("""() => {
            const text = document.body.innerText || '';
            // Look for signs of being logged in
            const hasAvatar = document.querySelectorAll('[class*="avatar"]').length > 0;
            const hasNewChat = text.includes('New Chat') || text.includes('新对话');
            return hasAvatar || hasNewChat;
        }""")

        browser_state["ready"] = True
        browser_state["login_window_open"] = False

        return jsonify({
            "success": True,
            "logged_in": is_logged_in,
            "message": "Session saved!"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    """Send message to Kimi"""
    data = request.json
    message = data.get("message", "")

    if not message:
        return jsonify({"error": "No message provided"}), 400

    if not browser_state["ready"] or not browser_state["page"]:
        return jsonify({"error": "Not ready. Please login first."}), 400

    try:
        page = browser_state["page"]

        # Ensure we're on Kimi
        if "kimi.com" not in page.url:
            page.goto(KIMI_URL)
            page.wait_for_load_state("networkidle")
            time.sleep(2)

        # Find input field
        input_selectors = [
            'div[contenteditable="true"]',
            'textarea',
            '[class*="editor"]',
            '[class*="input"] textarea',
        ]

        input_field = None
        for selector in input_selectors:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el and el.is_visible():
                    input_field = el
                    break
            except:
                continue

        if not input_field:
            return jsonify({"error": "Could not find chat input"}), 500

        # Click and clear
        input_field.click()
        time.sleep(0.3)

        # Type message
        input_field.fill("")
        time.sleep(0.1)
        input_field.type(message, delay=20)
        time.sleep(0.3)

        # Send
        page.keyboard.press("Enter")

        # Wait for response (poll for changes)
        time.sleep(3)

        # Get page content to find response
        response = ""
        start = time.time()
        last_text = ""

        while time.time() - start < 90:
            try:
                # Get all text blocks that look like messages
                texts = page.evaluate("""() => {
                    const results = [];
                    // Try multiple selectors for Kimi's response
                    const selectors = [
                        '[class*="markdown"]',
                        '[class*="message"]',
                        '[class*="content"]',
                        '[class*="answer"]',
                        '[class*="response"]',
                        '.chat-message',
                    ];
                    for (const sel of selectors) {
                        document.querySelectorAll(sel).forEach(el => {
                            const t = el.innerText?.trim();
                            if (t && t.length > 10) {
                                results.push(t);
                            }
                        });
                    }
                    return results;
                }""")

                if texts:
                    # Find the longest text (likely the full response)
                    longest = max(texts, key=len)
                    if longest and longest != message and len(longest) > 20:
                        # Check if still generating (look for loading indicators)
                        is_generating = page.evaluate("""() => {
                            const loaders = document.querySelectorAll(
                                '[class*="loading"], [class*="generating"], [class*="cursor"], .stop-generating'
                            );
                            return loaders.length > 0;
                        }""")

                        if not is_generating and longest == last_text:
                            response = longest
                            break
                        last_text = longest

            except Exception as e:
                print(f"Poll error: {e}")

            time.sleep(2)

        # Save storage
        save_current_storage()

        if response:
            return jsonify({"success": True, "response": response})
        else:
            return jsonify({"error": "No response received. Check if Kimi is working."}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/close", methods=["POST"])
def close_browser():
    global browser_state
    try:
        if browser_state["page"]:
            save_current_storage()
            browser_state["page"].close()
        if browser_state["context"]:
            browser_state["context"].close()
        if browser_state["browser"]:
            browser_state["browser"].close()
        if browser_state["pw"]:
            browser_state["pw"].stop()
    except:
        pass

    browser_state = {
        "pw": None, "browser": None, "context": None,
        "page": None, "ready": False, "login_window_open": False
    }

    return jsonify({"success": True})


if __name__ == "__main__":
    print("Starting Kimi Proxy...")
    print("Open http://localhost:5000 in your browser")
    app.run(debug=False, host="0.0.0.0", port=5000)
