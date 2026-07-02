"""
Dunamis v3 — keyless Gemini server + web chat, broadcastable on your network.

Exposes your Google-account Gemini as an OpenAI-compatible API on port 6970 with
NO API key and NO browser at runtime (pure HTTP via gemini_webapi, hardened with
curl_cffi Chrome impersonation, human pacing, and browser-like side-traffic).
Also serves a web tester chat at  /  (supports Ctrl+V image paste) and binds to
0.0.0.0 so other devices on your network can use it.

Auth: cookies in ~/.dunamis/gemini_cookies.json (env SECURE_1PSID/SECURE_1PSIDTS
override). Create them with the one-time login:  python -m dunamis.login
(or click "Log in" in the web chat).

Run:  python -m dunamis.server          (usually via the one-click launcher)
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, Response
from pydantic import BaseModel

WEB_DIR = Path(__file__).resolve().parent / "web"

from gemini_webapi import GeminiClient
from gemini_webapi.constants import Model
from gemini_webapi.exceptions import (
    AuthError, APIError, ModelInvalid, TemporarilyBlocked, UsageLimitExceeded,
    TimeoutError as GWTimeoutError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Dunamis.v3")

# Network-level Chrome impersonation (real TLS/JA3/JA4 + HTTP/2 via curl_cffi).
try:
    from . import curl_transport          # python -m dunamis.server
except Exception:
    try:
        import curl_transport             # direct-script fallback
    except Exception:
        curl_transport = None

# ─── Human pacing ─────────────────────────────────────────────────────────────
# Never fire requests back-to-back like an API. Each request waits a randomized
# "think + type" delay plus a minimum gap since the previous one (with jitter),
# so the request cadence looks human. Set DUNAMIS_PACE=0 to disable.
PACING = os.environ.get("DUNAMIS_PACE", "1").strip().lower() not in ("0", "false", "no", "off")
MIN_GAP = float(os.environ.get("DUNAMIS_MIN_GAP", "3.0"))   # min seconds between requests
_impersonation = False
_last_request_ts = 0.0

# ─── Behavioral side-traffic ──────────────────────────────────────────────────
# A real Gemini tab is never silent: it emits telemetry beacons while idle and
# fires several benign batchexecute RPCs around each message. A client that only
# ever sends StreamGenerate is anomalous by that silence. We replicate ONLY the
# two cheap, benign, correctly-formed signals (captured live), never the parts
# that need a real browser (WAA/BotGuard proofs, the signaler channel) — a
# forged proof is a worse tell than its absence. Best-effort; DUNAMIS_MIMIC=0 off.
MIMIC = os.environ.get("DUNAMIS_MIMIC", "1").strip().lower() not in ("0", "false", "no", "off")
_PLAY_LOG_URL = "https://play.google.com/log?format=json&hasfast=true&authuser=0"
_bg_tasks: set = set()
_mimic_task = None
_mimic_stats = {"beacons": 0, "warms": 0}

# Side-traffic intensity — kept LOW to minimize load on your Google account.
# WARM_PROB: chance a turn also fires a benign fetch_gems batchexecute.
# Beacons (harmless play/log telemetry) and idle warm-ups are widely spaced.
WARM_PROB = float(os.environ.get("DUNAMIS_WARM_PROB", "0.15"))
_BEACON_MIN, _BEACON_MAX = 90.0, 180.0     # seconds between idle telemetry beacons
_WARM_MIN, _WARM_MAX = 900.0, 1800.0       # seconds between idle benign batchexecute warm-ups

# ─── Model mapping ────────────────────────────────────────────────────────────
MODEL_MAP = {
    "flash": Model.G_3_0_FLASH, "gemini-3.0-flash": Model.G_3_0_FLASH, "fast": Model.G_3_0_FLASH,
    "pro": Model.G_3_0_PRO, "gemini-3.0-pro": Model.G_3_0_PRO,
    "thinking": Model.G_3_0_FLASH_THINKING, "think": Model.G_3_0_FLASH_THINKING,
    "gemini-3.0-thinking": Model.G_3_0_FLASH_THINKING,
    "gemini-3.0-flash-thinking": Model.G_3_0_FLASH_THINKING,
}
DEFAULT_MODEL = "gemini-3.0-flash"

COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".dunamis", "gemini_cookies.json")

client: Optional[GeminiClient] = None
_lock: Optional[asyncio.Lock] = None


def _load_cookies():
    """Cookies from env vars (preferred for phones) or the JSON file."""
    psid = os.environ.get("SECURE_1PSID")
    psidts = os.environ.get("SECURE_1PSIDTS")
    if psid:
        return psid, (psidts or "")
    if os.path.exists(COOKIE_FILE):
        data = json.load(open(COOKIE_FILE, encoding="utf-8"))
        return data.get("secure_1psid"), data.get("secure_1psidts", "")
    return None, None


def _load_full_jar():
    """Optional complete Google cookie jar (more browser-like). Saved by
    harvest_cookies.py under the 'all' key; absent when only the two auth
    cookies were entered by hand (which is enough to work)."""
    if os.path.exists(COOKIE_FILE):
        try:
            jar = json.load(open(COOKIE_FILE, encoding="utf-8")).get("all")
            if isinstance(jar, dict):
                return jar
        except Exception:
            pass
    return {}


def _inject_cookies(gemini_client, jar: dict):
    """Add the full jar to the live transport so every request carries it."""
    transport = getattr(gemini_client, "client", None)
    target = getattr(transport, "_cookies", None)
    if target is None:
        target = getattr(transport, "cookies", None)
    for name, value in jar.items():
        try:
            target.set(name, value, domain=".google.com")
        except TypeError:
            try:
                target.set(name, value)
            except Exception:
                pass
        except Exception:
            pass
    # Set on the GeminiClient's own jar too, but ALWAYS with an explicit domain:
    # a plain dict-update sets domainless cookies, which collide with the
    # .google.com ones already there and make by-name lookups raise
    # "Multiple cookies exist with name=__Secure-1PSID".
    for name, value in jar.items():
        try:
            gemini_client.cookies.set(name, value, domain=".google.com")
        except Exception:
            pass


async def _human_pace(prompt_len: int) -> float:
    """Sleep a randomized, human-like delay before sending; enforce a min gap
    since the previous request so we never burst like an API."""
    global _last_request_ts
    loop = asyncio.get_event_loop()
    if not PACING:
        _last_request_ts = loop.time()
        return 0.0
    think = random.uniform(0.8, 2.2)                                   # reading/deciding
    typing = min(prompt_len / random.uniform(300.0, 450.0), 6.0)      # "typing" the prompt
    since = loop.time() - _last_request_ts
    gap_wait = max(0.0, MIN_GAP - since)
    delay = max(think + typing, gap_wait) + random.uniform(0.0, 1.0)  # jitter
    await asyncio.sleep(delay)
    _last_request_ts = asyncio.get_event_loop().time()
    return delay


def _fire(coro):
    """Run a coroutine fire-and-forget, keeping a ref so it isn't GC'd."""
    t = asyncio.ensure_future(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


async def _beacon():
    """Fire-and-forget telemetry beacon mimicking the idle jslog heartbeat a real
    Gemini tab emits. Sent through the (impersonated) transport with our cookies;
    the response is irrelevant, so any failure is ignored."""
    try:
        transport = getattr(client, "client", None)
        if transport is None:
            return
        _mimic_stats["beacons"] += 1
        await asyncio.wait_for(transport.post(_PLAY_LOG_URL, data=b""), timeout=10)
    except Exception:
        pass


async def _warm_gems():
    """Benign, correctly-formed batchexecute (list gems) — a genuine browser call
    that also exercises the token/cookie path to keep the session warm."""
    try:
        _mimic_stats["warms"] += 1
        await asyncio.wait_for(client.fetch_gems(), timeout=15)
    except Exception:
        pass


async def _mimic_loop():
    """Background: emit human-cadence idle side-traffic for as long as we run."""
    try:
        await asyncio.sleep(random.uniform(2.0, 5.0))
        await _beacon()                       # 'page load' beacon
        loop = asyncio.get_event_loop()
        last_warm = loop.time()
        while True:
            await asyncio.sleep(random.uniform(_BEACON_MIN, _BEACON_MAX))
            await _beacon()
            # Occasionally (every ~15-30 min) do a real benign batchexecute warm.
            if loop.time() - last_warm > random.uniform(_WARM_MIN, _WARM_MAX):
                await _warm_gems()
                last_warm = loop.time()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def _warm_turn():
    """~60% of turns, fire a benign batchexecute concurrently with the send, like
    a browser brackets StreamGenerate with other RPCs."""
    if MIMIC and client is not None and random.random() < WARM_PROB:
        _fire(_warm_gems())


async def _swap_client(new):
    """Swap the global client under the generation lock so an in-flight request
    never has the client closed out from under it. Closes the old one after."""
    global client
    old = client
    if _lock is not None:
        async with _lock:
            client = new
    else:
        client = new
    if old is not None and old is not new:
        try:
            await old.close()
        except Exception:
            pass


async def _init_client() -> bool:
    """(Re)create the keyless Gemini client from saved cookies. Idempotent —
    called at startup and again after a fresh login. Builds the new client
    first, then swaps it in atomically. On failure the existing client is kept.
    Returns True on success."""
    global _impersonation, _mimic_task

    psid, psidts = _load_cookies()
    if not psid:
        logger.warning("Not logged in yet. Run  python -m dunamis.login  "
                       "or click 'Log in' in the web chat.")
        await _swap_client(None)
        return False

    if curl_transport is not None and not _impersonation:
        _impersonation = curl_transport.install()
        if _impersonation:
            logger.info("🛡️ Network impersonation ON (curl_cffi -> %s).",
                        curl_transport.DEFAULT_IMPERSONATE)
        else:
            logger.warning("⚠️ curl_cffi unavailable — using plain httpx (weaker fingerprint).")

    try:
        c = GeminiClient(psid, psidts or None)
        await c.init(timeout=60, auto_refresh=True, verbose=False)
        jar = _load_full_jar()
        if jar:
            _inject_cookies(c, jar)
    except Exception as e:
        logger.error("Failed to init Gemini client: %s (keeping existing client)", e)
        return False

    await _swap_client(c)
    logger.info("✅ Gemini client ready (keyless). impersonation=%s pacing=%s mimic=%s",
                "curl_cffi" if _impersonation else "httpx",
                "on" if PACING else "off", "on" if MIMIC else "off")
    if MIMIC and _mimic_task is None:
        _mimic_task = _fire(_mimic_loop())
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _lock, _mimic_task
    _lock = asyncio.Lock()
    await _init_client()
    yield
    if _mimic_task:
        _mimic_task.cancel()
    for t in list(_bg_tasks):
        t.cancel()
    if client:
        try:
            await client.close()
        except Exception:
            pass


app = FastAPI(title="Dunamis v3", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

_login_lock: Optional[asyncio.Lock] = None


# ─── Web chat + status/login ──────────────────────────────────────────────────
@app.get("/")
async def web_chat():
    idx = WEB_DIR / "index.html"
    if idx.is_file():
        # no-store so a reload always gets the latest UI (never a stale page)
        return FileResponse(str(idx), headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "web UI not found"}, status_code=404)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/status")
async def status():
    return {"logged_in": client is not None and getattr(client, "running", True),
            "impersonation": "curl_cffi" if _impersonation else "httpx",
            "pacing": "on" if PACING else "off",
            "mimic": "on" if MIMIC else "off",
            "model": DEFAULT_MODEL}


@app.post("/login")
async def login():
    """Run the one-time Google login (opens a real browser window on the machine
    running the server), then re-initialize the keyless client from the fresh
    cookies. Note: the login window opens where the SERVER runs."""
    global _login_lock
    if _login_lock is None:
        _login_lock = asyncio.Lock()
    if _login_lock.locked():
        return {"status": "in_progress"}
    async with _login_lock:
        logger.info("🔐 Launching login window (python -m dunamis.login)...")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "dunamis.login",
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            await asyncio.wait_for(proc.wait(), timeout=360)
        except asyncio.TimeoutError:
            return {"status": "timeout", "logged_in": False}
        except Exception as e:
            return {"status": "error", "detail": str(e), "logged_in": False}
        ok = await _init_client()
        return {"status": "ok" if ok else "failed", "logged_in": ok}


@app.post("/logout")
async def logout():
    """Sign out: drop the in-memory client, delete saved cookies, and wipe the
    browser login profile so the next 'Log in' is a fresh sign-in — letting you
    choose a DIFFERENT Google account."""
    import shutil
    await _swap_client(None)
    _sessions.clear()
    cleared = []
    try:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
            cleared.append("cookies")
    except Exception as e:
        logger.warning("logout: could not remove cookies file: %s", e)
    profile = os.path.join(os.path.expanduser("~"), ".dunamis", "chrome-profile-v3")
    if os.path.isdir(profile):
        shutil.rmtree(profile, ignore_errors=True)
        cleared.append("browser profile")
    logger.info("🔓 Logged off (cleared: %s).", ", ".join(cleared) or "nothing")
    return {"status": "ok", "logged_in": False, "cleared": cleared}


# ─── OpenAI-compatible models ─────────────────────────────────────────────────
class ContentPart(BaseModel):
    type: str = "text"
    text: Optional[str] = None
    image_url: Optional[dict] = None   # OpenAI vision: {"url": "data:...;base64,..." | "http..."}

class ChatMessage(BaseModel):
    role: str
    content: str | list[ContentPart]

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = DEFAULT_MODEL
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    response_format: Optional[dict] = None
    tools: Optional[list] = None
    tool_choice: Optional[str | dict] = None


# ─── Helpers (ported, trimmed, from server.py) ────────────────────────────────
def _schema_instruction(response_format: dict) -> str:
    if not response_format:
        return ""
    if response_format.get("type") == "json_schema":
        js = response_format.get("json_schema", {})
        schema = js.get("schema", js.get("strict_schema", {}))
        if schema:
            return ("\n\n[OUTPUT FORMAT] Respond with ONLY a valid JSON object — no "
                    "markdown, no code fences, no extra text — matching this schema "
                    f"({js.get('name', 'Response')}):\n{json.dumps(schema, indent=2)}\n"
                    "Use exactly these field names. Output ONLY the JSON.")
    if response_format.get("type") == "json_object":
        return ("\n\n[OUTPUT FORMAT] Respond with ONLY a valid JSON object. No markdown, "
                "no code fences, no explanation.")
    return ""


def _build_prompt(messages: List[ChatMessage], response_format: dict = None) -> str:
    parts = []
    for msg in messages:
        if isinstance(msg.content, str):
            text = msg.content
        elif isinstance(msg.content, list):
            text = "\n".join(p.text for p in msg.content if p.type == "text" and p.text)
        else:
            continue
        if msg.role == "system":
            parts.append(f"[SYSTEM INSTRUCTIONS]\n{text}\n[END SYSTEM INSTRUCTIONS]")
        elif msg.role == "assistant":
            parts.append(f"Assistant: {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts) + _schema_instruction(response_format)


# ─── Image ingestion (multimodal input) ───────────────────────────────────────
# OpenAI vision clients attach images as content parts:
#   {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
# gemini_webapi's send_message(..., files=[paths]) uploads local files, so we
# decode data: URLs (and best-effort download http[s] URLs) to temp files and
# pass them alongside the prompt. Images ride on the latest user turn.
_IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp"}


def _data_url_to_bytes(url: str):
    header, _, data = url.partition(",")
    if not data:
        return None, None
    mime = "image/png"
    meta = header[5:] if header.startswith("data:") else header
    if meta:
        mime = meta.split(";", 1)[0] or mime
    try:
        raw = base64.b64decode(data) if ";base64" in header else data.encode("utf-8")
    except Exception:
        return None, None
    return raw, mime


async def _url_to_bytes(url: str):
    try:
        transport = getattr(client, "client", None)
        if transport is None:
            return None, None
        r = await asyncio.wait_for(transport.get(url), timeout=30)
        if getattr(r, "status_code", 0) != 200:
            return None, None
        mime = "image/png"
        try:
            mime = (r.headers.get("content-type", "") or "").split(";")[0].strip() or mime
        except Exception:
            pass
        return r.content, mime
    except Exception:
        return None, None


async def _extract_image_files(request):
    """Write images from the latest user message to temp files. Returns
    (paths, cleanup). Supports data: URLs (base64) and http(s) URLs."""
    last = None
    for m in reversed(request.messages):
        if m.role == "user":
            last = m
            break
    paths = []
    if last is not None and isinstance(last.content, list):
        for part in last.content:
            if part.type not in ("image_url", "input_image") or not part.image_url:
                continue
            url = part.image_url.get("url") if isinstance(part.image_url, dict) else None
            if not url:
                continue
            if url.startswith("data:"):
                raw, mime = _data_url_to_bytes(url)
            elif url.startswith("http"):
                raw, mime = await _url_to_bytes(url)
            else:
                raw, mime = None, None
            if not raw:
                logger.warning("image part skipped (could not decode/fetch)")
                continue
            fd, p = tempfile.mkstemp(suffix=_IMG_EXT.get((mime or "").lower(), ".png"),
                                     prefix="dunamis-img-")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            paths.append(p)

    def cleanup():
        for p in paths:
            try:
                os.unlink(p)
            except Exception:
                pass

    return paths, cleanup


# ─── Conversation continuity ──────────────────────────────────────────────────
# The OpenAI API is stateless — clients resend the whole history each call. To
# make Gemini see ONE ongoing conversation (more correct AND more human than
# starting a fresh chat every turn), we map each conversation to a persistent
# gemini_webapi ChatSession, keyed by a hash of the history-so-far + model. A
# continued turn then sends only the NEW user message to the existing session.
MAX_SESSIONS = int(os.environ.get("DUNAMIS_MAX_SESSIONS", "64"))
_sessions: "OrderedDict[str, object]" = OrderedDict()


def _msg_text(m) -> str:
    if isinstance(m.content, str):
        return m.content
    if isinstance(m.content, list):
        return "\n".join(p.text for p in m.content if p.type == "text" and p.text)
    return ""


def _img_digest(m) -> str:
    """Short fingerprint of a message's attached images, so two turns with the
    same text but different images don't collide to the same ChatSession."""
    urls = []
    if isinstance(m.content, list):
        for p in m.content:
            if p.type in ("image_url", "input_image") and isinstance(p.image_url, dict):
                urls.append(p.image_url.get("url") or "")
    if not urls:
        return ""
    return hashlib.sha256("|".join(urls).encode("utf-8", "ignore")).hexdigest()[:16]


def _norm(messages) -> list:
    return [(m.role, _msg_text(m), _img_digest(m)) for m in messages]


def _conv_key(norm_prefix: list, model_id: str) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([model_id, norm_prefix], ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def _remember(key: str, chat) -> None:
    _sessions[key] = chat
    _sessions.move_to_end(key)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def _resolve_session(request, model, model_id):
    """Return (chat, send_text, norm).

    chat: a ChatSession to send `send_text` to. For a continued turn `send_text`
    is just the new user message; for a new/diverged conversation it's the full
    history (which seeds the fresh session's context).
    """
    norm = _norm(request.messages)
    rf = request.response_format
    if not norm or norm[-1][0] != "user":
        # No trailing user turn — just send the whole thing in a fresh session.
        return client.start_chat(model=model), _build_prompt(request.messages, rf), norm
    prefix = norm[:-1]
    # .get (not .pop): keep the session cached if this generation fails, so a
    # retry with the same history still continues instead of re-seeding.
    chat = _sessions.get(_conv_key(prefix, model_id))
    if chat is not None:
        # Continuation: the session already holds the context.
        return chat, _msg_text(request.messages[-1]) + _schema_instruction(rf), norm
    # New (or diverged / cold-start) conversation: seed with the full history.
    return client.start_chat(model=model), _build_prompt(request.messages, rf), norm


def _store_next(norm: list, reply_text: str, model_id: str, chat) -> None:
    """Index the session under the key the client's NEXT request will produce
    (current history + our assistant reply)."""
    if not norm or norm[-1][0] != "user" or not reply_text:
        return
    _remember(_conv_key(norm + [("assistant", reply_text)], model_id), chat)


def _http_from_exc(e: Exception) -> HTTPException:
    """Map gemini_webapi exceptions to sensible HTTP responses."""
    if isinstance(e, AuthError):
        return HTTPException(401, f"Auth/cookies expired: {e}. Re-run harvest_cookies.py "
                                  "or refresh SECURE_1PSID/SECURE_1PSIDTS.")
    if isinstance(e, UsageLimitExceeded):
        return HTTPException(429, f"Gemini usage limit reached: {e}")
    if isinstance(e, ModelInvalid):
        return HTTPException(400, f"Model unavailable/inconsistent: {e}")
    if isinstance(e, TemporarilyBlocked):
        return HTTPException(503, "Gemini temporarily blocked this request "
                                 "(rate / anti-abuse). Wait a bit and retry.")
    if isinstance(e, GWTimeoutError):
        return HTTPException(504, f"Gemini timed out: {e}")
    return HTTPException(502, f"Gemini error: {e}")


async def _send_with_retries(chat, send_text, model, files=None, retries=2):
    """Send via the ChatSession with backoff on transient blocks/timeouts."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            return await chat.send_message(send_text, files=files or None)
        except (TemporarilyBlocked, GWTimeoutError):
            if attempt >= retries:
                raise
        except APIError as e:
            # 1013 is a known transient Gemini error that clears on retry.
            if attempt >= retries or not ("1013" in str(e) or "temporar" in str(e).lower()):
                raise
        await asyncio.sleep(delay + random.uniform(0.0, 1.0))
        delay *= 2


def _clean_response(text: str) -> str:
    """Light cleanup. gemini_webapi already returns clean markdown, so we only
    unwrap a JSON payload when one is clearly present (for structured output)."""
    if not text:
        return text
    cleaned = re.sub(r"```\w*\n?", "", text.strip())
    s = cleaned.strip()
    if s and s[0] in "{[":
        brace = bracket = 0
        end = -1
        for i, ch in enumerate(s):
            if ch == "{": brace += 1
            elif ch == "}":
                brace -= 1
                if brace == 0 and bracket == 0: end = i
            elif ch == "[": bracket += 1
            elif ch == "]":
                bracket -= 1
                if bracket == 0 and brace == 0: end = i
        if end > 0:
            cleaned = s[:end + 1]
    return cleaned.strip()


def _sse_chunk(text, model, cid):
    d = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
         "model": model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    return f"data: {json.dumps(d)}\n\n"


def _sse_done(model, cid):
    d = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
         "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(d)}\n\ndata: [DONE]\n\n"


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if client and getattr(client, "running", True):
        return {"status": "healthy", "service": "Dunamis v3",
                "browser": "connected", "engine": "keyless-webapi",
                "impersonation": "curl_cffi" if _impersonation else "httpx",
                "pacing": "on" if PACING else "off",
                "mimic": "on" if MIMIC else "off",
                "mimic_stats": dict(_mimic_stats),
                "current_model": DEFAULT_MODEL}
    return {"status": "degraded", "service": "Dunamis v3", "browser": "not ready",
            "engine": "keyless-webapi",
            "hint": "Not logged in — run  python -m dunamis.login  or click 'Log in' in the web chat."}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [
        {"id": "gemini-3.0-flash", "object": "model", "owned_by": "google"},
        {"id": "gemini-3.0-thinking", "object": "model", "owned_by": "google"},
        {"id": "gemini-3.0-pro", "object": "model", "owned_by": "google"},
    ]}


# Generation timeouts so a stalled upstream surfaces an error instead of an
# endless spinner. CHUNK = max wait for the next streamed piece; GEN = total.
CHUNK_TIMEOUT = float(os.environ.get("DUNAMIS_CHUNK_TIMEOUT", "90"))
GEN_TIMEOUT = float(os.environ.get("DUNAMIS_GEN_TIMEOUT", "300"))


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    if not client:
        raise HTTPException(status_code=503,
                            detail="Gemini client not initialized — check cookies.")
    if not request.messages:
        raise HTTPException(status_code=400, detail="No messages provided.")

    model_id = request.model or DEFAULT_MODEL
    model = MODEL_MAP.get(model_id.lower(), Model.G_3_0_FLASH)
    rid = f"dunamis-light-{int(time.time())}"

    chat, send_text, norm = _resolve_session(request, model, model_id)
    files, cleanup = await _extract_image_files(request)
    if not (send_text or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty prompt.")
    if files:
        # gemini_webapi wants a non-empty prompt even for image-only turns.
        if not (send_text or "").strip():
            send_text = "Describe this image."
        logger.info("🖼️ %d image(s) attached to this turn", len(files))
    logger.info("📝 request: model=%s, send=%d chars, stream=%s, turns=%d",
                model_id, len(send_text), request.stream, len(norm))

    # ── Streaming: hold the lock for the whole generation and emit real deltas ──
    if request.stream:
        async def gen():
            acc = ""
            try:
                async with _lock:
                    waited = await _human_pace(len(send_text))
                    logger.info("⏱️ human pacing: waited %.1fs before sending", waited)
                    _warm_turn()
                    agen = chat.send_message_stream(send_text, files=files or None).__aiter__()
                    started = time.time()
                    while True:
                        try:
                            out = await asyncio.wait_for(agen.__anext__(), timeout=CHUNK_TIMEOUT)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise GWTimeoutError("Gemini stopped responding")
                        if time.time() - started > GEN_TIMEOUT:
                            raise GWTimeoutError("response took too long")
                        cur = out.text or ""
                        if len(cur) > len(acc):
                            delta = cur[len(acc):]
                            acc = cur
                            yield _sse_chunk(delta, model_id, rid)
                    # Key the next turn on exactly what the client received (the
                    # raw streamed text it will echo back as the assistant turn).
                    _store_next(norm, acc, model_id, chat)
                yield _sse_done(model_id, rid)
            except Exception as e:
                logger.error("stream error: %s", e)
                detail = _http_from_exc(e).detail
                yield f"data: {json.dumps({'error': detail})}\n\n"
                yield _sse_done(model_id, rid)
            finally:
                cleanup()
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── Non-streaming ──
    try:
        async with _lock:
            waited = await _human_pace(len(send_text))
            logger.info("⏱️ human pacing: waited %.1fs before sending", waited)
            _warm_turn()
            try:
                out = await asyncio.wait_for(
                    _send_with_retries(chat, send_text, model, files=files),
                    timeout=GEN_TIMEOUT)
            except asyncio.TimeoutError:
                raise HTTPException(504, "Gemini did not respond in time — your session may have "
                                         "expired. Try 'Log in' again, or retry.")
            except Exception as e:
                logger.error("generate failed: %s", e)
                raise _http_from_exc(e)
            text = _clean_response(out.text or "")
            _store_next(norm, text, model_id, chat)
    finally:
        cleanup()

    return {
        "id": rid, "object": "chat.completion", "created": int(time.time()),
        "model": model_id,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(send_text.split()),
                  "completion_tokens": len(text.split()),
                  "total_tokens": len(send_text.split()) + len(text.split())},
    }


@app.post("/v1/chat/new")
async def new_chat():
    """Drop all cached conversations so the next request starts fresh sessions."""
    n = len(_sessions)
    _sessions.clear()
    return {"status": "ok", "message": f"Cleared {n} cached conversation(s)."}


def _lan_ips():
    """Best-effort list of this machine's LAN IPv4 addresses."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


if __name__ == "__main__":
    import argparse
    import uvicorn
    p = argparse.ArgumentParser(description="Dunamis v3 — keyless Gemini server + web chat")
    p.add_argument("--port", type=int, default=6970, help="Port (default 6970)")
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind host (0.0.0.0 = broadcast on your network)")
    args = p.parse_args()

    port = args.port
    print("\n" + "=" * 62)
    print("  Dunamis v3 — your Gemini, broadcasting on your network")
    print("=" * 62)
    print(f"  Open the chat here : http://localhost:{port}/")
    for ip in _lan_ips():
        print(f"  From other devices : http://{ip}:{port}/   (same Wi-Fi / LAN)")
    print(f"  OpenAI API base    : http://<this-address>:{port}/v1   (any dummy key)")
    print("=" * 62 + "\n")
    uvicorn.run(app, host=args.host, port=port, log_level="info")
