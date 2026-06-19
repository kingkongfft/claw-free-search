#!/usr/bin/env python3
"""
Simple Kimi AI Search Script
Usage:
  First time: python kimi_search.py --login
  Search: python kimi_search.py "your search query"
"""

import json
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


# Configuration
KIMI_URL = "https://www.kimi.com"
AUTH_FILE = Path("kimi_auth.json")
COOKIES_FILE = Path("kimi_cookies.json")


def load_auth():
    """Load saved authentication data"""
    cookies = []
    if COOKIES_FILE.exists():
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    return cookies


def save_cookies(cookies):
    """Save cookies to file"""
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    print(f"✓ Saved {len(cookies)} cookies to {COOKIES_FILE}")


def interactive_login():
    """Perform interactive login"""
    print("\n=== Kimi Interactive Login ===")
    print("A browser window will open. Please login manually.")
    print("After successful login, press Enter in this terminal to continue.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Navigate to Kimi
        page.goto(KIMI_URL)
        print(f"Opened {KIMI_URL}")
        print("Please login in the browser window...")

        # Wait for user to login
        input("\nPress Enter after you have successfully logged in...")

        # Extract cookies
        cookies = context.cookies()
        save_cookies(cookies)

        browser.close()

    print("\n✓ Login completed!")
    return cookies


def search_kimi(query: str, cookies: list) -> str:
    """Perform a search using Kimi"""
    print(f"\n🔍 Searching: {query}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Add saved cookies
        if cookies:
            context.add_cookies(cookies)
            print(f"✓ Loaded {len(cookies)} cookies")

        page = context.new_page()
        page.goto(KIMI_URL)

        # Wait for page to load
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        # Look for input field and type query
        try:
            # Try different selectors for the input field
            selectors = [
                'textarea',
                '[contenteditable="true"]',
                'input[type="text"]',
                '[class*="input"]',
                '[class*="editor"]',
                '[class*="chat"]'
            ]

            input_field = None
            for selector in selectors:
                try:
                    input_field = page.wait_for_selector(selector, timeout=3000)
                    if input_field:
                        print(f"✓ Found input field with selector: {selector}")
                        break
                except:
                    continue

            if input_field:
                # Click and type query
                input_field.click()
                time.sleep(0.5)
                input_field.fill("")
                input_field.type(query, delay=50)

                # Press Enter to send
                page.keyboard.press("Enter")

                # Wait for response
                print("⏳ Waiting for response...")
                time.sleep(8)

                # Get page content
                response = page.inner_text("body")

                # Save updated cookies
                new_cookies = context.cookies()
                save_cookies(new_cookies)

                browser.close()
                return response
            else:
                print("❌ Could not find input field")
                browser.close()
                return None

        except Exception as e:
            print(f"❌ Error during search: {e}")
            browser.close()
            return None


def main():
    """Main function"""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  Login: python kimi_search.py --login")
        print("  Search: python kimi_search.py \"your search query\"")
        sys.exit(1)

    if sys.argv[1] == "--login":
        interactive_login()
    else:
        query = " ".join(sys.argv[1:])
        cookies = load_auth()

        if not cookies:
            print("No saved authentication found. Please login first.")
            print("Run: python kimi_search.py --login")
            sys.exit(1)

        result = search_kimi(query, cookies)
        if result:
            print("\n=== Search Result ===")
            print(result[:3000])  # Print first 3000 chars


if __name__ == "__main__":
    main()
