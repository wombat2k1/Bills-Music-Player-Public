"""Network helpers."""
import ssl
import urllib.request

from .config import ALLOW_INSECURE_TLS_FALLBACK


def http_get(url: str, timeout: int = 10) -> bytes:
    ctx = ssl.create_default_context()
    headers = {"User-Agent": "BillsMusicPlayer/1.0 (local)"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except Exception:
        # Retry once with TLS 1.2 minimum and a fresh context
        ctx = ssl.create_default_context()
        if hasattr(ssl, "TLSVersion"):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception:
            if not ALLOW_INSECURE_TLS_FALLBACK:
                raise
            # Last-resort fallback for environments with TLS interception issues
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
