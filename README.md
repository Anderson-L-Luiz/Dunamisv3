# Dunamis v3

Turn your **Google-account Gemini** into a private, **OpenAI-compatible local LLM
server** with a one-click launch — on **Windows, macOS, or Ubuntu**. No API key.
It broadcasts on your network and ships a built-in **web tester chat** that
supports **Ctrl+V image paste**.

```
You (or any device on your Wi-Fi)
        │  http://<this-pc>:6970/         ← web chat (Ctrl+V images)
        │  http://<this-pc>:6970/v1       ← OpenAI-compatible API (any dummy key)
        ▼
Dunamis v3  ──►  gemini.google.com   (your logged-in account, keyless)
```

## Quick start (one click)

**Prerequisite:** Python **3.11+** installed and on PATH
([python.org/downloads](https://www.python.org/downloads/) — on Windows tick
*"Add Python to PATH"*). On Ubuntu 22.04 (ships 3.10):
`sudo apt install python3.12 python3.12-venv`. The launcher auto-selects a
3.11+ interpreter if several are installed.

| OS | Do this |
|----|---------|
| **Windows** | double-click **`Dunamis.bat`** |
| **macOS** | double-click **`Dunamis.command`** (first time: right-click → Open, or `chmod +x Dunamis.command`) |
| **Ubuntu/Linux** | `chmod +x Dunamis-Linux.sh` then double-click (choose *Run*) or `./Dunamis-Linux.sh` |

The first launch automatically creates a private `.venv`, installs everything,
and opens a window to **sign in to your Google account** (one time). Then the
web chat opens in your browser. Later launches skip straight to starting.

That's it — keep the console window open; close it to stop the server.

## The web chat (with Ctrl+V images)

Opens at `http://localhost:6970/`. You can:
- **Paste images with Ctrl+V** (also drag-and-drop, or the 📎 button), then ask about them.
- Stream replies live, switch model (Flash / Pro / Thinking), and start a new chat.
- It keeps conversation context (one ongoing Gemini conversation, not a fresh one each turn).

## Broadcast on your network

The server binds `0.0.0.0`, so other devices on the same Wi-Fi/LAN can use it.
On startup it prints the addresses, e.g.:

```
Open the chat here : http://localhost:6970/
From other devices : http://192.168.1.177:6970/
OpenAI API base    : http://<this-address>:6970/v1   (any dummy key)
```

Point **any OpenAI-compatible app** at `http://<this-address>:6970/v1` (use any
non-empty API key). Models: `gemini-3.0-flash`, `gemini-3.0-pro`,
`gemini-3.0-thinking` (aliases `flash` / `pro` / `thinking`).

> ⚠️ The API has **no authentication** — only broadcast on a trusted network.
> To reach it remotely, put it behind a tunnel (Tailscale / Cloudflare Tunnel).

## Use it from code

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:6970/v1", api_key="dunamis")
print(client.chat.completions.create(
    model="gemini-3.0-flash",
    messages=[{"role": "user", "content": "Hello!"}],
).choices[0].message.content)
```

Vision (image) input uses the standard OpenAI format
(`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}` or an
http(s) URL).

## What's under the hood

Keyless and light at runtime (pure HTTP via `gemini_webapi`; a browser is used
**only** for the one-time login). It's hardened to look like a real browser:

- **Chrome TLS/HTTP-2 fingerprint** via `curl_cffi` (impersonates Chrome 145).
- **Full cookie jar** + Chrome client-hint headers.
- **Human pacing** (randomized think/type delay + min gap; never bursts).
- **Browser-like side-traffic** (idle telemetry beacon + benign batchexecute).
- **Streaming**, **conversation continuity**, **image input**, and resilient
  error handling (auth-expiry → clear message, retries on transient blocks).

Honest limits: it does **not** forge BotGuard/WAA JS proofs (needs a real
browser) — it works because Gemini currently accepts cookie-authenticated
requests. Personal-use, moderate-volume, trusted-network tool.

## Config (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `DUNAMIS_PORT` | `6970` | port |
| `DUNAMIS_PACE` | `1` | human pacing on/off |
| `DUNAMIS_MIN_GAP` | `3` | min seconds between requests |
| `DUNAMIS_MIMIC` | `1` | browser-like side-traffic on/off |
| `DUNAMIS_IMPERSONATE` | `chrome145` | curl_cffi target |
| `SECURE_1PSID` / `SECURE_1PSIDTS` | – | provide cookies directly (skip login) |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Python not found" | Install Python 3.11+ and ensure it's on PATH |
| Chat says "not logged in" | Click **Log in**, or run `python -m dunamis.login` |
| Session expired later | Same — log in again (the launcher/UI will prompt) |
| Port 6970 in use | `set DUNAMIS_PORT=6971` (Win) / `export DUNAMIS_PORT=6971` then relaunch |
| macOS "cannot be opened" | right-click `Dunamis.command` → Open (once) |

## Files

```
Dunamis.bat / Dunamis.command / Dunamis-Linux.sh   one-click launchers
launcher.py                                         cross-platform bootstrap (.venv + deps + login + run)
requirements.txt
dunamis/
  server.py         keyless API + web chat + broadcast + /login
  login.py          one-time Google login (harvests session cookies)
  curl_transport.py Chrome TLS/HTTP2 impersonation transport
  chat.py           optional CLI chat (has /image too)
  web/index.html    the web tester chat (Ctrl+V images)
```

## Notes

- The **login window opens on the machine running the server.** Run it on your
  own desktop; other devices then connect over the network.
- Not affiliated with Google. Automates your own logged-in session; use responsibly.
