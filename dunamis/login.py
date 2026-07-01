"""
Dunamis v3 — one-time Google login.

Opens a real Chrome window to gemini.google.com and waits for you to sign in to
your Google account. Once you're in, it harvests the session cookies to
~/.dunamis/gemini_cookies.json and closes. After this the server runs keyless
(pure HTTP, no browser) using those cookies.

Run:
    python -m dunamis.login          (or: python dunamis/login.py)

Re-run any time your session expires (the server will say "not logged in").
"""
import asyncio
import json
import os
import sys

PROFILE = os.path.join(os.path.expanduser("~"), ".dunamis", "chrome-profile-v3")
OUT = os.path.join(os.path.expanduser("~"), ".dunamis", "gemini_cookies.json")
EDITOR = '.ql-editor[contenteditable="true"]'


async def main(timeout_s: int = 300) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is required for login:  pip install playwright && playwright install chromium")
        return 2

    os.makedirs(PROFILE, exist_ok=True)
    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check"],
            viewport={"width": 1180, "height": 820},
        )
    except Exception as e:
        print(f"Could not launch Chromium: {e}\nRun:  playwright install chromium")
        await pw.stop()
        return 2

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    print("\n=== Sign in to your Google account in the window that opened. ===")
    print("Waiting for you to reach the Gemini chat page… (up to %d s)\n" % timeout_s)

    logged_in = False
    for _ in range(timeout_s // 2):
        try:
            if await page.query_selector(EDITOR):
                logged_in = True
                break
        except Exception:
            pass
        await asyncio.sleep(2)

    cookies = await ctx.cookies("https://gemini.google.com")
    jar = {c["name"]: c["value"] for c in cookies}
    await ctx.close()
    await pw.stop()

    psid = jar.get("__Secure-1PSID")
    if not psid:
        print("Login not detected / no session cookie found. Make sure you finished signing in, then re-run.")
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"secure_1psid": psid,
                   "secure_1psidts": jar.get("__Secure-1PSIDTS", ""),
                   "all": jar}, f, indent=2)
    print(f"\n✅ Logged in — saved {len(jar)} cookies to {OUT}")
    if not logged_in:
        print("(Note: didn't detect the chat editor, but a session cookie was captured. "
              "If the server says not logged in, re-run login.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
