#!/usr/bin/env python3
"""
Kimi AI Authentication & Search Script
This script handles Kimi AI login, token management, and search functionality.
"""

import json
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page


# Configuration
KIMI_URL = "https://www.kimi.com"
AUTH_FILE = Path("kimi_auth.json")
COOKIES_FILE = Path("kimi_cookies.json")


class KimiAuth:
    def __init__(self):
        self.auth_data = {}
        self.cookies = []
        self.load_auth()

    def load_auth(self):
        """Load saved authentication data"""
        if AUTH_FILE.exists():
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                self.auth_data = json.load(f)
            print(f"✓ Loaded auth data from {AUTH_FILE}")

        if COOKIES_FILE.exists():
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                self.cookies = json.load(f)
            print(f"✓ Loaded cookies from {COOKIES_FILE}")

    def save_auth(self):
        """Save authentication data"""
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(self.auth_data, f, indent=2, ensure_ascii=False)
        print(f"✓ Saved auth data to {AUTH_FILE}")

        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cookies, f, indent=2, ensure_ascii=False)
        print(f"✓ Saved cookies to {COOKIES_FILE}")

    def is_authenticated(self) -> bool:
        """Check if we have valid authentication"""
        return bool(self.cookies) or bool(self.auth_data.get("access_token"))

    def interactive_login(self):
        """Perform interactive login using Playwright browser"""
        print("\n=== Kimi Interactive Login ===")
        print("A browser window will open. Please login manually.")
        print("After successful login, press Enter in this terminal to continue.\n")

        with sync_playwright() as p:
            # Launch browser with persistent context
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
            self.cookies = context.cookies()
            print(f"✓ Captured {len(self.cookies)} cookies")

            # Extract auth tokens from localStorage/sessionStorage
            auth_tokens = page.evaluate("""() => {
                const tokens = {};
                // Check localStorage
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    if (key.includes('token') || key.includes('auth') || key.includes('session') || key.includes('user')) {
                        tokens[key] = localStorage.getItem(key);
                    }
                }
                // Check sessionStorage
                for (let i = 0; i < sessionStorage.length; i++) {
                    const key = sessionStorage.key(i);
                    if (key.includes('token') || key.includes('auth') || key.includes('session') || key.includes('user')) {
                        tokens['session_' + key] = sessionStorage.getItem(key);
                    }
                }
                return tokens;
            }""")

            self.auth_data = {
                "tokens": auth_tokens,
                "url": page.url,
                "timestamp": time.time()
            }

            # Save auth data
            self.save_auth()

            browser.close()

        print("\n✓ Login completed! Auth data saved.")
        return True

    def get_headers(self) -> dict:
        """Get headers for API requests"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": KIMI_URL,
            "Origin": KIMI_URL,
        }

        # Add auth token if available
        if self.auth_data.get("tokens"):
            for key, value in self.auth_data["tokens"].items():
                if "token" in key.lower() and value:
                    headers["Authorization"] = f"Bearer {value}"
                    break

        return headers

    def get_cookies_dict(self) -> dict:
        """Get cookies as dictionary"""
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}


class KimiSearch:
    def __init__(self, auth: KimiAuth):
        self.auth = auth
        self.context = None
        self.page = None

    def start_browser(self):
        """Start browser with saved authentication"""
        if not self.auth.is_authenticated():
            print("❌ Not authenticated. Please run login first.")
            return False

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=False)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        # Add saved cookies
        if self.auth.cookies:
            self.context.add_cookies(self.auth.cookies)

        self.page = self.context.new_page()
        self.page.goto(KIMI_URL)
        print(f"✓ Opened {KIMI_URL} with saved authentication")
        return True

    def search(self, query: str) -> str:
        """Perform a search using Kimi"""
        if not self.page:
            print("❌ Browser not started. Call start_browser() first.")
            return None

        print(f"\n🔍 Searching: {query}")

        # Wait for the input field to be ready
        try:
            # Look for the chat input field
            input_field = self.page.wait_for_selector('textarea, [contenteditable="true"], input[type="text"]', timeout=10000)
            if input_field:
                # Clear and type the query
                input_field.click()
                input_field.fill("")
                input_field.type(query, delay=50)

                # Press Enter to send
                self.page.keyboard.press("Enter")

                # Wait for response
                print("⏳ Waiting for response...")
                time.sleep(5)

                # Try to get the response
                response = self.page.evaluate("""() => {
                    // Look for response elements
                    const responseElements = document.querySelectorAll('[class*="response"], [class*="message"], [class*="answer"], [class*="content"]');
                    let response = '';
                    responseElements.forEach(el => {
                        if (el.textContent && el.textContent.length > 10) {
                            response += el.textContent + '\\n';
                        }
                    });
                    return response || document.body.innerText;
                }""")

                return response
            else:
                print("❌ Could not find input field")
                return None

        except Exception as e:
            print(f"❌ Error during search: {e}")
            return None

    def close(self):
        """Close browser and save updated cookies"""
        if self.context:
            # Save updated cookies
            self.auth.cookies = self.context.cookies()
            self.auth.save_auth()

        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()


def main():
    """Main function"""
    print("=== Kimi AI Authentication & Search ===\n")

    auth = KimiAuth()

    # Check if we need to login
    if not auth.is_authenticated():
        print("No saved authentication found.")
        auth.interactive_login()
    else:
        print("✓ Found saved authentication")
        print(f"  Last updated: {time.ctime(auth.auth_data.get('timestamp', 0))}")

    # Ask user what to do
    print("\nWhat would you like to do?")
    print("1. Test search")
    print("2. Re-login")
    print("3. Exit")

    choice = input("\nEnter your choice (1-3): ").strip()

    if choice == "1":
        # Test search
        search = KimiSearch(auth)
        if search.start_browser():
            query = input("\nEnter your search query: ").strip()
            if query:
                result = search.search(query)
                if result:
                    print("\n=== Search Result ===")
                    print(result[:2000])  # Print first 2000 chars
            search.close()

    elif choice == "2":
        auth.interactive_login()

    elif choice == "3":
        print("Goodbye!")
        return

    print("\n✓ Done!")


if __name__ == "__main__":
    main()
