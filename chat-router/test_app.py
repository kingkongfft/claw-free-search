"""
Automated tests for chat-router Flask app.

Tests the UI rendering and all REST API endpoints without requiring a real
Kimi login (the chat and confirm endpoints are tested for correct error
responses when the browser is not open).
"""

import json
import sys
import traceback
from playwright.sync_api import sync_playwright

BASE = "http://localhost:5000"
PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    indicator = "[+]" if condition else "[!]"
    safe_detail = detail.encode("ascii", errors="replace").decode("ascii")
    print(
        f"  {indicator} [{status}] {name}"
        + (f" -- {safe_detail}" if safe_detail else "")
    )


def run_tests(page):
    # ------------------------------------------------------------------
    # 1. Landing page renders correctly
    # ------------------------------------------------------------------
    print("\n--- UI: Landing page ---")
    page.goto(BASE)
    page.wait_for_load_state("networkidle")

    check(
        "Page title is 'Kimi Proxy'",
        page.title() == "Kimi Proxy",
        f"got: {page.title()!r}",
    )
    check("H1 heading present", page.locator("h1").count() > 0)
    check(
        "'Open Kimi Login' button visible",
        page.locator("button#btn-open-kimi").is_visible(),
    )
    check(
        "'I've logged in' button exists",
        page.locator("button#btn-confirm-login").count() > 0,
    )
    check(
        "Login section visible on first load",
        page.locator("#login-section").is_visible(),
    )
    check(
        "Chat section hidden on first load",
        not page.locator("#chat-section").is_visible(),
    )

    # ------------------------------------------------------------------
    # 2. GET /api/status
    # ------------------------------------------------------------------
    print("\n--- API: GET /api/status ---")
    resp = page.request.get(f"{BASE}/api/status")
    check("Status 200", resp.status == 200, f"got {resp.status}")
    body = resp.json()
    check("browser_ready key present", "browser_ready" in body)
    check(
        "browser_ready is False (no login yet)",
        body.get("browser_ready") is False,
        f"got {body.get('browser_ready')!r}",
    )
    check("storage_exists key present", "storage_exists" in body)

    # ------------------------------------------------------------------
    # 3. POST /api/login/confirm — should fail gracefully (no browser open)
    # ------------------------------------------------------------------
    print("\n--- API: POST /api/login/confirm (no browser) ---")
    resp = page.request.post(f"{BASE}/api/login/confirm")
    check(
        "Returns 4xx when no browser open",
        400 <= resp.status < 500,
        f"got {resp.status}",
    )
    body = resp.json()
    check("Error message returned", "error" in body, f"body: {body}")

    # ------------------------------------------------------------------
    # 4. POST /api/chat — should fail gracefully (not logged in)
    # ------------------------------------------------------------------
    print("\n--- API: POST /api/chat (not logged in) ---")
    resp = page.request.post(
        f"{BASE}/api/chat",
        data=json.dumps({"message": "hello"}),
        headers={"Content-Type": "application/json"},
    )
    check(
        "Returns 4xx when not logged in", 400 <= resp.status < 500, f"got {resp.status}"
    )
    body = resp.json()
    check("Error message returned", "error" in body, f"body: {body}")

    # ------------------------------------------------------------------
    # 5. POST /api/chat — missing message body
    # ------------------------------------------------------------------
    print("\n--- API: POST /api/chat (missing message) ---")
    resp = page.request.post(
        f"{BASE}/api/chat",
        data=json.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    check("Returns 400 for empty message", resp.status == 400, f"got {resp.status}")
    body = resp.json()
    check(
        "Error message says 'No message'",
        "No message" in body.get("error", ""),
        f"body: {body}",
    )

    # ------------------------------------------------------------------
    # 6. POST /api/close — should succeed (idempotent)
    # ------------------------------------------------------------------
    print("\n--- API: POST /api/close (idempotent) ---")
    resp = page.request.post(f"{BASE}/api/close")
    check("Returns 200", resp.status == 200, f"got {resp.status}")
    body = resp.json()
    check("success: true", body.get("success") is True, f"body: {body}")

    # ------------------------------------------------------------------
    # 7. Status indicator reflects no-login state in the UI
    # ------------------------------------------------------------------
    print("\n--- UI: Status indicator ---")
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)  # let JS checkStatus() settle
    status_text = page.locator("#status-indicator").inner_text()
    check(
        "Status indicator shows 'Not Logged In'",
        "Not Logged In" in status_text or "Checking" in status_text,
        f"got: {status_text!r}",
    )

    # ------------------------------------------------------------------
    # 8. POST /api/login — should open browser (or return 200 + success)
    # ------------------------------------------------------------------
    print("\n--- API: POST /api/login ---")
    resp = page.request.post(f"{BASE}/api/login")
    check("Returns 200", resp.status == 200, f"got {resp.status}")
    body = resp.json()
    check("success: true", body.get("success") is True, f"body: {body}")

    # Immediately close so we don't leave a dangling browser window
    page.request.post(f"{BASE}/api/close")
    check("Cleaned up after login test", True)


def main():
    print("=" * 60)
    print("Kimi Proxy -- automated test suite")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            run_tests(page)
        except Exception:
            print("\n[FATAL] Unhandled exception during tests:")
            traceback.print_exc()
        finally:
            browser.close()

    # Summary
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
