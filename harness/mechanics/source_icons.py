"""Bounded public favicon reads with DNS pinning and a small process cache."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import io
import re
import ssl
import time
from urllib.parse import urljoin, urlsplit

import httpcore
from PIL import Image

from custom_http import public_addresses, validated_addresses, _PinnedBackend
from public_urls import validate_url

MAX_BYTES = 65536
FALLBACK = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg>'


def normalized_host(host):
    if (not isinstance(host, str) or not 3 <= len(host) <= 253 or host != host.lower()
            or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})', host)):
        raise ValueError("Choose a public website domain.")
    return urlsplit(validate_url(f"https://{host}/favicon.ico")).hostname


def raster_icon(body):
    with Image.open(io.BytesIO(body)) as image:
        if image.format not in {"PNG", "ICO", "JPEG", "WEBP", "GIF"} or not (0 < image.width <= 512 and 0 < image.height <= 512):
            raise ValueError("Choose a small website icon.")
        image.load()
        result = image.convert("RGBA")
        result.thumbnail((64, 64))
        result.info.clear()
        output = io.BytesIO()
        result.save(output, format="PNG")
        return output.getvalue()


class SourceIcons:
    def __init__(self, *, resolver=public_addresses, network_backend=None, ssl_context=None, clock=time.monotonic):
        self.resolver, self.backend, self.clock = resolver, network_backend, clock
        self.ssl = ssl_context or ssl.create_default_context()
        if self.ssl.verify_mode != ssl.CERT_REQUIRED or not self.ssl.check_hostname:
            raise ValueError("Verified TLS is required.")
        self.cache = OrderedDict()
        self.inflight = set()

    async def get(self, host):
        host = normalized_host(host)
        cached = self.cache.get(host)
        if cached and cached[0] > self.clock():
            self.cache.move_to_end(host)
            return cached[1:]
        if host in self.inflight or len(self.inflight) >= 4:
            return FALLBACK, "image/svg+xml"
        self.inflight.add(host)
        body, kind, ttl = FALLBACK, "image/svg+xml", 600
        try:
            async with asyncio.timeout(6):
                body = raster_icon(await self._fetch(f"https://{host}/favicon.ico"))
                kind, ttl = "image/png", 21600
        except Exception:
            # A source remains readable when its optional icon is unavailable.
            pass
        finally:
            self.inflight.discard(host)
        self.cache[host] = (self.clock() + ttl, body, kind)
        self.cache.move_to_end(host)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return body, kind

    async def _fetch(self, url):
        for hop in range(3):
            url = validate_url(url)
            host = urlsplit(url).hostname
            addresses = validated_addresses(await self.resolver(host))
            backend = _PinnedBackend(host, addresses, lambda: True, self.backend)
            async with httpcore.AsyncConnectionPool(ssl_context=self.ssl, network_backend=backend,
                    max_connections=1, max_keepalive_connections=0, retries=0) as pool:
                async with pool.stream("GET", url, headers=[(b"host", host.encode()), (b"accept-encoding", b"identity")],
                        extensions={"timeout": {"connect": 2, "read": 2, "write": 2, "pool": 1}}) as response:
                    if len(response.headers) > 64 or sum(len(k) + len(v) for k, v in response.headers) > 16384:
                        raise ValueError("Invalid icon response.")
                    if response.status in {301, 302, 303, 307, 308}:
                        locations = [v for k, v in response.headers if k.lower() == b"location"]
                        if len(locations) != 1 or hop == 2:
                            raise ValueError("Invalid icon redirect.")
                        url = urljoin(url, locations[0].decode("ascii"))
                        continue
                    headers = {k.lower(): v for k, v in response.headers}
                    if response.status != 200 or headers.get(b"content-encoding", b"identity") != b"identity":
                        raise ValueError("Website icon unavailable.")
                    chunks, size = [], 0
                    async for chunk in response.aiter_stream():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Website icon exceeds the display limit.")
                        chunks.append(chunk)
                    return b"".join(chunks)
        raise ValueError("Website icon unavailable.")
