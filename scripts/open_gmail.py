"""Open Gmail in a headed Playwright Chrome browser using the existing profile.

After logging in, press Enter in this terminal to save the authentication state
to storage_state.json, which benchmark.py uses for Google auth.
"""

from playwright.sync_api import sync_playwright

PROFILE_DIR = "playwright_chrome_profile"
STORAGE_STATE_FILE = "storage_state.json"

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        executable_path="/Applications/Web Browsers/Google Chrome.app/Contents/MacOS/Google Chrome",
        headless=False,
        args=[
            "--profile-directory=Default",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )

    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://mail.google.com", wait_until="domcontentloaded", timeout=300_000)

    print("Browser opened at Gmail. Log in if needed.")
    print(f"When logged in, press Enter here to save auth state to '{STORAGE_STATE_FILE}' and close.")

    input()

    browser.storage_state(path=STORAGE_STATE_FILE)
    print(f"Auth state saved to '{STORAGE_STATE_FILE}'.")

    browser.close()
    print("Browser closed.")
