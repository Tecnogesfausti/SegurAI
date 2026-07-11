from __future__ import annotations

import contextlib
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

from tools.common import compact_json


async def web_fetch_url(context: Any, args: dict[str, Any]) -> str:
    httpx = getattr(context, "httpx", None)
    if httpx is None:
        raise RuntimeError("httpx no esta disponible")
    url = str(args.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Solo se permiten URLs publicas http/https")
    host = parsed.hostname or ""
    validate_public_web_host(host)
    max_chars = max(1000, min(int(args.get("max_chars", 12000) or 12000), 50000))
    user_headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    headers = {"User-Agent": f"{getattr(context, 'app_name', 'SegurAI')}/1.0"}
    for key, value in user_headers.items():
        if str(key).lower() in {"authorization", "cookie", "proxy-authorization"}:
            continue
        headers[str(key)] = str(value)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
        response = await http.get(url, headers=headers)
        final_host = response.url.host or ""
        validate_public_web_host(final_host)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        text = response.text
    extracted = extract_web_text(text, content_type)
    return compact_json(
        {
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "truncated": len(extracted) > max_chars,
            "text": extracted[:max_chars],
        },
        max_chars=max_chars + 2000,
    )


def validate_public_web_host(host: str) -> None:
    lowered = host.lower().strip("[]")
    if (
        lowered in {"localhost", "ip6-localhost"}
        or lowered.endswith((".localhost", ".local", ".lan", ".home", ".internal"))
    ):
        raise PermissionError("Destino web local no permitido")
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(lowered)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise PermissionError("Destino web privado/local no permitido")


def extract_web_text(text: str, content_type: str) -> str:
    if "html" not in content_type.lower():
        return text
    cleaned = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = cleaned.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n{3,}", "\n\n", cleaned)).strip()
