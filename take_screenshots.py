#!/usr/bin/env python3
"""Take screenshots of all Second Brain pages.

Usage:
    pip install playwright
    playwright install chromium
    python take_screenshots.py

Requires the server to be running on localhost:8000.
"""

import subprocess
import sys
import time

PAGES = [
    ("dashboard", "/"),
    ("knowledge-web", "/knowledge"),
    ("graph-view", "/graph"),
    ("hub", "/hub"),
    ("skills", "/skills"),
    ("calendar", "/calendar"),
    ("browser", "/browser"),
    ("colleagues", "/colleagues"),
    ("settings", "/settings"),
]

def check_server():
    """Check if the server is running."""
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:8000/health", timeout=5)
        return True
    except Exception:
        return False

def take_screenshots():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Installing playwright...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        # Set the API key in localStorage to bypass auth
        page.goto("http://localhost:8000/")
        page.evaluate("localStorage.setItem('brain_api_key', 'sb_78b845d31739d90389a06891d9e3e87eacceca2267b40ac0')")

        for name, route in PAGES:
            url = f"http://localhost:8000{route}"
            print(f"Capturing {name}... ", end="", flush=True)
            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
                time.sleep(2)  # Wait for animations to settle
                page.screenshot(path=f"screenshots/{name}.png", full_page=False)
                print("✓")
            except Exception as e:
                print(f"✗ ({e})")

        browser.close()
        print(f"\nDone! Screenshots saved to screenshots/")

if __name__ == "__main__":
    if not check_server():
        print("❌ Server not running. Start it first: python app/main.py")
        sys.exit(1)
    print("📸 Taking screenshots of all pages...\n")
    take_screenshots()
