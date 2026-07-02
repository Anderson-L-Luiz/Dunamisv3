"""
curl_cffi transport for gemini_webapi — network-level browser impersonation.

Pure-Python httpx (what gemini_webapi uses by default) has a Python/OpenSSL
TLS handshake and an httpx HTTP/2 frame layout. To Google those are an obvious
"not a browser" signal no matter how good the headers are. curl_cffi uses
curl-impersonate's patched BoringSSL to reproduce a real Chrome's exact TLS
(JA3/JA4) and HTTP/2 (Akamai) fingerprint — AND it injects Chrome's own default
headers (sec-ch-ua, sec-fetch-*, accept-language, ...) consistent with that TLS.

`install()` monkeypatches the `AsyncClient` symbol in every gemini_webapi module
that makes outbound Google requests, swapping httpx for a thin adapter
(`CurlCffiClient`) that speaks the small subset of the httpx API gemini_webapi
relies on, backed by a curl_cffi AsyncSession impersonating Chrome.

If curl_cffi isn't installed (e.g. on Termux/Android where the native binary may
not build), `install()` returns False and the caller should fall back to plain
httpx (headers + pacing hardening only).
"""
import os
from contextlib import asynccontextmanager

# Impersonation target. Pinned to chrome145 to match CHROME_MAJOR used elsewhere;
# override with DUNAMIS_IMPERSONATE (e.g. "chrome131_android" on a phone).
DEFAULT_IMPERSONATE = os.environ.get("DUNAMIS_IMPERSONATE", "chrome145")


def _u(url):
    """Underlying URL string. gemini_webapi passes Endpoint members, which are
    str-subclass Enums: str(member) yields 'Endpoint.X', so bypass Enum.__str__
    and return the real string value."""
    try:
        return str.__str__(url)
    except Exception:
        return "%s" % (url,)


def _to_multipart(files):
    """Convert httpx-style files={field: (filename, content[, content_type])} into
    a curl_cffi CurlMime (curl_cffi 0.13 requires `multipart`, not `files`)."""
    import mimetypes as _mt
    from curl_cffi import CurlMime
    mp = CurlMime()
    for field, spec in files.items():
        fname, content, ctype = None, spec, None
        if isinstance(spec, (tuple, list)):
            fname = spec[0] if len(spec) > 0 else None
            content = spec[1] if len(spec) > 1 else b""
            ctype = spec[2] if len(spec) > 2 else None
        if hasattr(content, "read"):
            content = content.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not ctype:
            ctype = _mt.guess_type(fname or "")[0] or "application/octet-stream"
        mp.addpart(name=field, filename=fname or field, data=content, content_type=ctype)
    return mp


def _cookie_dict(httpx_cookies):
    """name->value dict from an httpx.Cookies (single-domain use here)."""
    try:
        return {c.name: c.value for c in httpx_cookies.jar}
    except Exception:
        try:
            return dict(httpx_cookies)
        except Exception:
            return {}


def _resp_cookie_dict(resp):
    """name->value dict of Set-Cookie from a curl_cffi response."""
    d = {}
    try:
        for c in resp.cookies.jar:
            d[c.name] = c.value
    except Exception:
        try:
            d = dict(resp.cookies)
        except Exception:
            d = {}
    return d


class _Resp:
    """Minimal httpx.Response stand-in for non-streaming calls."""
    def __init__(self, r):
        self._r = r

    @property
    def status_code(self):
        return self._r.status_code

    @property
    def text(self):
        return self._r.text

    @property
    def content(self):
        return self._r.content

    @property
    def headers(self):
        return self._r.headers

    @property
    def cookies(self):
        # dict supports .get(name), which is all gemini_webapi uses.
        return _resp_cookie_dict(self._r)

    def json(self, **kw):
        return self._r.json()

    def raise_for_status(self):
        # Delegate to curl_cffi so non-2xx actually raises (gemini_webapi relies
        # on this to catch failed uploads / token fetches instead of silently
        # using an error body).
        return self._r.raise_for_status()


class _StreamResp:
    """Minimal streaming-response stand-in (exposes .status_code + aiter_bytes)."""
    def __init__(self, r, client):
        self._r = r
        self._client = client

    @property
    def status_code(self):
        return self._r.status_code

    @property
    def cookies(self):
        return _resp_cookie_dict(self._r)

    async def aiter_bytes(self, chunk_size=None):
        async for chunk in self._r.aiter_content():
            yield chunk
        self._client._capture(self._r)


class CurlCffiClient:
    """httpx.AsyncClient-compatible adapter backed by curl_cffi (Chrome impersonation).

    Implements only what gemini_webapi touches: construction kwargs, async-context
    use, .get/.post, .stream (async CM yielding aiter_bytes), .cookies, .aclose.
    """
    def __init__(self, impersonate=None, timeout=300, proxy=None, headers=None,
                 cookies=None, verify=None, **kwargs):
        import httpx
        from curl_cffi.requests import AsyncSession

        self._impersonate = impersonate or DEFAULT_IMPERSONATE
        self._timeout = timeout if isinstance(timeout, (int, float)) else 300
        self._proxy = proxy
        self._verify = True if verify is None else verify

        # Drop any caller User-Agent so curl_cffi's impersonation UA (which is
        # consistent with the impersonated TLS + client hints) wins.
        self._base_headers = {}
        for k, v in dict(headers or {}).items():
            if k.lower() == "user-agent":
                continue
            self._base_headers[k] = v

        # httpx.Cookies as the source of truth so gemini_webapi's
        # `self.cookies.update(self.client.cookies)` and `.update(...)` work.
        self._cookies = httpx.Cookies()
        if cookies is not None:
            try:
                self._cookies.update(cookies)
            except Exception:
                pass

        self._session = AsyncSession()

    # -- cookies -------------------------------------------------------------
    @property
    def cookies(self):
        return self._cookies

    def _capture(self, resp):
        for name, value in _resp_cookie_dict(resp).items():
            try:
                self._cookies.set(name, value)
            except Exception:
                pass

    def _headers(self, extra):
        h = dict(self._base_headers)
        if extra:
            h.update(dict(extra))
        return h

    def _common(self, extra_cookies=None):
        jar = _cookie_dict(self._cookies)
        if extra_cookies is not None:
            # A per-call jar (e.g. rotate_1psidts passes cookies=) must be honored.
            try:
                jar.update(_cookie_dict(extra_cookies))      # httpx.Cookies
            except Exception:
                try:
                    jar.update(dict(extra_cookies))
                except Exception:
                    pass
        return dict(impersonate=self._impersonate, timeout=self._timeout,
                    proxy=self._proxy, verify=self._verify, cookies=jar)

    # -- requests ------------------------------------------------------------
    async def get(self, url, params=None, headers=None, cookies=None, **kw):
        r = await self._session.request("GET", _u(url), params=params,
                                        headers=self._headers(headers),
                                        **self._common(cookies))
        self._capture(r)
        return _Resp(r)

    async def post(self, url, params=None, data=None, headers=None, files=None,
                   json=None, follow_redirects=None, content=None, cookies=None, **kw):
        extra = {}
        if files is not None:            # multipart (e.g. file upload)
            extra["multipart"] = _to_multipart(files)
        if json is not None:
            extra["json"] = json
        if follow_redirects is not None:
            extra["allow_redirects"] = follow_redirects
        # httpx callers pass a raw body as `content` (rotate_1psidts does);
        # curl_cffi has no `content` kwarg, it uses `data`.
        if data is None and json is None and files is None and content is not None:
            data = content
        r = await self._session.request("POST", _u(url), params=params, data=data,
                                        headers=self._headers(headers),
                                        **self._common(cookies), **extra)
        self._capture(r)
        return _Resp(r)

    @asynccontextmanager
    async def stream(self, method, url, params=None, headers=None, data=None,
                     cookies=None, **kw):
        async with self._session.stream(method, _u(url), params=params, data=data,
                                        headers=self._headers(headers),
                                        **self._common(cookies)) as r:
            self._capture(r)   # capture Set-Cookie at headers time, like httpx
            yield _StreamResp(r, self)

    # -- lifecycle -----------------------------------------------------------
    async def aclose(self):
        try:
            await self._session.close()
        except Exception:
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


def install(impersonate=None):
    """Patch gemini_webapi to route all Google traffic through curl_cffi.

    Returns True on success, False if curl_cffi is unavailable (caller should
    then keep using plain httpx).
    """
    target = impersonate or DEFAULT_IMPERSONATE
    try:
        import curl_cffi  # noqa: F401
    except Exception:
        return False

    def factory(*args, **kwargs):
        kwargs.setdefault("impersonate", target)
        return CurlCffiClient(*args, **kwargs)

    import gemini_webapi.client as gclient
    gclient.AsyncClient = factory
    for modpath in ("gemini_webapi.utils.get_access_token",
                    "gemini_webapi.utils.rotate_1psidts",
                    "gemini_webapi.utils.upload_file",
                    "gemini_webapi.types.image"):
        try:
            mod = __import__(modpath, fromlist=["AsyncClient"])
            if hasattr(mod, "AsyncClient"):
                mod.AsyncClient = factory
        except Exception:
            pass
    return True
