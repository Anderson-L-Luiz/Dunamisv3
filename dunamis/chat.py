"""
Dunamisv2 — simple interactive CLI chatbot for testing the Gemini proxy.

Talks to the local OpenAI-compatible server (default http://127.0.0.1:6970).
Keeps the conversation history so you get multi-turn context, and lets you
switch models on the fly.

Run directly (assumes the server is already up):
    python chat.py
Or just double-click  "Dunamis Chatbot.bat"  on the Desktop, which starts the
server for you first.

Commands inside the chat:
    /model <flash|pro|thinking|flash-lite>   switch the Gemini model
    /image <path>                            attach an image to your next message
    /reset                                   clear the conversation history
    /health                                  show server/browser status
    /exit  (or /quit, Ctrl+C)                leave
"""
import base64
import mimetypes
import os
import sys
import time

try:
    import requests
except ImportError:
    print("The 'requests' package is required:  pip install requests")
    sys.exit(1)

BASE = "http://127.0.0.1:6970"
MODEL = "flash"          # flash | pro | thinking | flash-lite
VALID_MODELS = {"flash", "fast", "pro", "thinking", "think", "flash-lite", "lite"}

BANNER = r"""
  ____                              _     ____
 |  _ \ _   _ _ __   __ _ _ __ ___ (_)___|___ \
 | | | | | | | '_ \ / _` | '_ ` _ \| / __| __) |
 | |_| | |_| | | | | (_| | | | | | | \__ \/ __/
 |____/ \__,_|_| |_|\__,_|_| |_| |_|_|___/_____|   Gemini chatbot (test)
"""


def health():
    try:
        return requests.get(BASE + "/health", timeout=5).json()
    except Exception as e:
        return {"status": "unreachable", "error": str(e)}


def wait_for_server(timeout=120):
    print("[chat] Connecting to the Gemini proxy", end="", flush=True)
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        last = health()
        if last.get("browser") == "connected":
            print(" connected.\n")
            return last
        print(".", end="", flush=True)
        time.sleep(2)
    print(" (could not confirm a connected browser)\n")
    return last


def ask(history):
    payload = {"model": MODEL, "messages": history, "stream": False}
    t0 = time.time()
    try:
        r = requests.post(BASE + "/v1/chat/completions", json=payload, timeout=900)
        data = r.json()
    except Exception as e:
        return None, "[request error: %s]" % e, time.time() - t0
    dt = time.time() - t0
    if isinstance(data, dict) and "error" in data:
        return None, "[error: %s]" % data["error"], dt
    try:
        return data["choices"][0]["message"]["content"], None, dt
    except Exception:
        return None, "[unexpected response: %s]" % str(data)[:300], dt


def main():
    global MODEL
    print(BANNER)
    h = wait_for_server()
    print("Server : %s | browser: %s | model on page: %s"
          % (h.get("status", "?"), h.get("browser", "?"), h.get("current_model", "?")))
    if h.get("browser") != "connected":
        print("\n  WARNING: the Gemini browser is not connected.")
        print("  If you were logged out of Google, run once:")
        print("      python server.py --visible      (log in, then restart)\n")
    print("Commands: /model <..>  /image <path>  /reset  /health  /exit")
    print("Model    : %s    (responses can take ~20-60s; deep models longer)\n" % MODEL)

    history = []
    pending_image = None   # data: URL attached to the next message
    while True:
        try:
            user = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break
        if not user:
            continue

        low = user.lower()
        if low in ("/exit", "/quit", ":q"):
            print("bye!")
            break
        if low == "/reset":
            history = []
            pending_image = None
            print("(conversation cleared)\n")
            continue
        if low == "/health":
            print("  %s\n" % health())
            continue
        if low.startswith("/model"):
            parts = user.split()
            if len(parts) >= 2 and parts[1].lower() in VALID_MODELS:
                MODEL = parts[1].lower()
                print("(model -> %s)\n" % MODEL)
            else:
                print("(current model: %s ; choices: flash, pro, thinking, flash-lite)\n" % MODEL)
            continue
        if low.startswith("/image"):
            path = user[len("/image"):].strip().strip('"')
            if not path or not os.path.isfile(path):
                print("(usage: /image <path-to-image-file>)\n")
                continue
            mime = mimetypes.guess_type(path)[0] or "image/png"
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            pending_image = "data:%s;base64,%s" % (mime, b64)
            print("(image attached: %s - will send with your next message)\n"
                  % os.path.basename(path))
            continue

        if pending_image:
            content = [{"type": "text", "text": user},
                       {"type": "image_url", "image_url": {"url": pending_image}}]
            pending_image = None
        else:
            content = user
        history.append({"role": "user", "content": content})
        print("gemini > thinking ... (please wait)", flush=True)
        content, err, dt = ask(history)
        if err:
            history.pop()  # drop the unanswered turn
            print("gemini > %s   (%.0fs)\n" % (err, dt))
            continue
        print("gemini > %s" % content)
        print("         [%.0fs - %s]\n" % (dt, MODEL))
        history.append({"role": "assistant", "content": content})


if __name__ == "__main__":
    main()
