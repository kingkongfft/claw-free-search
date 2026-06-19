"""
Smoke test for chat-router with free providers (Google AI, DuckDuckGo AI, Perplexity).

Tests the API endpoints and actual provider responses.
Server must be running on http://localhost:5000 first.
"""

import json
import sys
import time
import requests

BASE = "http://localhost:5000"
PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    indicator = "[+]" if condition else "[!]"
    print(f"  {indicator} [{status}] {name}" + (f" -- {detail}" if detail else ""))


def test_providers():
    """GET /api/providers should return all 3 free providers, all ready."""
    print("\n--- API: GET /api/providers ---")
    resp = requests.get(f"{BASE}/api/providers", timeout=5)
    check("Status 200", resp.status_code == 200, f"got {resp.status_code}")
    body = resp.json()
    check("Has 'google' provider", "google" in body, f"keys: {list(body.keys())}")
    check("Has 'duckduckgo' provider", "duckduckgo" in body)
    check("Has 'perplexity' provider", "perplexity" in body)
    for k in ["google", "duckduckgo", "perplexity"]:
        if k in body:
            check(f"{k} is ready", body[k].get("ready") is True)


def test_status():
    """GET /api/status should report all providers ready."""
    print("\n--- API: GET /api/status ---")
    resp = requests.get(f"{BASE}/api/status", timeout=5)
    check("Status 200", resp.status_code == 200, f"got {resp.status_code}")
    body = resp.json()
    check("any_ready is True", body.get("any_ready") is True)


def test_chat_empty():
    """POST /api/chat with empty message should return 400."""
    print("\n--- API: POST /api/chat (empty) ---")
    resp = requests.post(f"{BASE}/api/chat", json={"message": ""}, timeout=5)
    check("Returns 400", resp.status_code == 400, f"got {resp.status_code}")


def test_chat_provider(provider: str, query: str = "hello"):
    """POST /api/chat to a specific provider and check the response."""
    print(f"\n--- API: POST /api/chat -> {provider} ({query}) ---")
    t0 = time.time()
    try:
        resp = requests.post(
            f"{BASE}/api/chat",
            json={"message": query, "provider": provider},
            timeout=180,
        )
        elapsed = time.time() - t0
        body = resp.json()
        check(f"{provider}: Status 200", resp.status_code == 200, f"got {resp.status_code}, {elapsed:.1f}s")

        if resp.status_code == 200 and body.get("success"):
            response_text = body.get("response", "")
            check(
                f"{provider}: Got response text",
                len(response_text) > 10,
                f"length={len(response_text)}, preview: {response_text[:120]!r}",
            )
        else:
            error = body.get("error", body.get("_client_error", "unknown"))
            check(f"{provider}: Got error", False, error)
    except requests.Timeout:
        check(f"{provider}: Timed out", False, f">{180}s")
    except Exception as e:
        check(f"{provider}: Exception", False, str(e))


def test_close():
    """POST /api/close should succeed."""
    print("\n--- API: POST /api/close ---")
    resp = requests.post(f"{BASE}/api/close", timeout=10)
    check("Returns 200", resp.status_code == 200, f"got {resp.status_code}")
    body = resp.json()
    check("success: true", body.get("success") is True, f"body: {body}")


def main():
    print("=" * 60)
    print("Chat Router (Free Providers) -- smoke test")
    print("=" * 60)

    # Basic API tests
    test_providers()
    test_status()
    test_chat_empty()

    # Live provider tests — these actually hit the external sites
    test_chat_provider("duckduckgo", "hello")
    test_chat_provider("google", "hello")
    test_chat_provider("perplexity", "hello")

    # Cleanup
    test_close()

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
