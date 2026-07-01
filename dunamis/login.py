"""
Dunamis v3 — one-time Google login.

Opens a real Chrome window to gemini.google.com and waits for you to sign in to
your Google account. It watches for the actual Google **session cookie**
(__Secure-1PSID) to appear, so the window stays open the whole time you're
signing in and only closes once you're genuinely logged in. Then it harvests the
cookies to ~/.dunamis/gemini_cookies.json and the server runs keyless from there.

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


def _valid_psid(jar: dict):
    """A real signed-in __Secure-1PSID is long; logged-out visits don't set it."""
    v = jar.get("__Secure-1PSID") or ""
    return v if len(v) > 40 else None


async def main(timeout_s: int = 300) -> int:
    # Windows consoles default to cp1252 and choke on non-ASCII; keep output safe.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is required for login:  pip install playwright && playwright install chromium")
        return 2

    os.makedirs(PROFILE, exist_ok=True)
    # Clear any stale single-instance lock from a previous run so we can reopen.
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(PROFILE, lock))
        except OSError:
            pass

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
        await page.goto("https://gemini.google.com/app",
                        wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    print("\n" + "=" * 64, flush=True)
    print("  Sign in to your Google account in the window that just opened.")
    print("  Leave it open - it closes by itself once you're signed in.")
    print("  (waits up to %d seconds)" % timeout_s)
    print("=" * 64 + "\n", flush=True)

    psid = None
    jar = {}
    for _ in range(max(1, timeout_s // 2)):
        # Stop early if the user closed the window.
        if not ctx.pages:
            break
        try:
            cookies = await ctx.cookies("https://gemini.google.com")
            jar = {c["name"]: c["value"] for c in cookies}
            if _valid_psid(jar):
                # Let the session settle so __Secure-1PSIDTS is captured too.
                await asyncio.sleep(2)
                if ctx.pages:
                    cookies = await ctx.cookies("https://gemini.google.com")
                    jar = {c["name"]: c["value"] for c in cookies}
                psid = _valid_psid(jar)
                if psid:
                    break
        except Exception:
            pass
        await asyncio.sleep(2)

    try:
        await ctx.close()
    except Exception:
        pass
    await pw.stop()

    if not psid:
        print("\nLogin not completed - no session cookie was captured.")
        print("Re-run and finish signing in (the window closes on its own once you're in).")
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"secure_1psid": psid,
                   "secure_1psidts": jar.get("__Secure-1PSIDTS", ""),
                   "all": jar}, f, indent=2)
    print(f"\n[OK] Signed in - saved {len(jar)} cookies. Return to the chat; it's ready.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
