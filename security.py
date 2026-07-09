"""Small, dependency-free security helpers for BackroomsGPT Atlas.

The API has public retrieval routes and a smaller group of state-changing Atlas
routes.  These helpers keep their authentication, rate limiting, and discovery
target validation consistent without putting secret values in the OpenAPI file.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import socket
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit


class URLSafetyError(ValueError):
    """A caller supplied a URL outside the discovery security boundary."""


def is_unsafe_ip(value: str) -> bool:
    """Return whether an address is unsuitable as a server-side fetch target."""
    ip = ipaddress.ip_address(value)
    # `100.64.0.0/10` is neither `is_private` nor `is_global` in Python's
    # ipaddress module. Treat every non-global address as unsafe so this and
    # future special-purpose allocations cannot become accidental fetch targets.
    return (not ip.is_global) or any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def normalize_allowed_http_url(
    url: str,
    *,
    explicit_hosts: Iterable[str],
    allowed_suffixes: Iterable[str],
    allowed_ports: Iterable[int] = (80, 443),
) -> str:
    """Validate syntax and the public platform allowlist before DNS resolution."""
    parsed = urlsplit((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise URLSafetyError("Only http and https URLs are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise URLSafetyError("URL userinfo is not allowed.")
    if not parsed.hostname:
        raise URLSafetyError("Target URL has no hostname.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLSafetyError("Target URL has an invalid port.") from exc
    if port is not None and port not in set(allowed_ports):
        raise URLSafetyError("Target URL port is not allowed.")

    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        raise URLSafetyError("Local hostnames are not allowed.")
    try:
        if is_unsafe_ip(host):
            raise URLSafetyError("Private, local, multicast, reserved, and unspecified addresses are not allowed.")
    except ValueError:
        pass

    explicit = {item.casefold().rstrip(".") for item in explicit_hosts}
    suffixes = tuple(item.casefold() for item in allowed_suffixes)
    if host not in explicit and not any(host.endswith(suffix) for suffix in suffixes):
        raise URLSafetyError("Target host is not in the supported Backrooms wiki allowlist.")

    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


Resolver = Callable[[str, int], Awaitable[list]]


async def _default_resolver(host: str, port: int) -> list:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def validate_discovery_target(
    url: str,
    *,
    explicit_hosts: Iterable[str],
    allowed_suffixes: Iterable[str],
    resolver: Optional[Resolver] = None,
) -> str:
    """Validate an allowlisted discovery target and every DNS result.

    Discovery is intentionally limited to Fandom/Wikidot platform domains.  DNS
    validation rejects a host when *any* A/AAAA record is unsafe.  The caller
    must still perform manual redirect validation; this helper validates each
    destination passed to it.
    """
    normalized = normalize_allowed_http_url(
        url,
        explicit_hosts=explicit_hosts,
        allowed_suffixes=allowed_suffixes,
    )
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    lookup = resolver or _default_resolver
    try:
        records = await lookup(host, port)
    except (OSError, socket.gaierror) as exc:
        raise URLSafetyError("Target hostname could not be resolved safely.") from exc

    addresses = {record[4][0] for record in records if len(record) >= 5 and record[4]}
    if not addresses:
        raise URLSafetyError("Target hostname did not resolve to an address.")
    for address in addresses:
        try:
            if is_unsafe_ip(address):
                raise URLSafetyError("Target hostname resolves to an unsafe address.")
        except ValueError as exc:
            raise URLSafetyError("Target hostname returned an invalid address.") from exc
    return normalized


class ActionAuth:
    """Bearer-token verifier for protected Custom GPT Action routes."""

    def __init__(self, env_name: str = "BACKROOMSGPT_ACTION_API_KEY") -> None:
        self.env_name = env_name

    def _configured_secret(self) -> str:
        """Read the deployment secret without retaining or exposing its value.

        Reading on demand also supports secret rotation in a long-lived worker
        when the process environment is refreshed by its host/test harness.
        """
        return os.getenv(self.env_name, "")

    @property
    def configured(self) -> bool:
        return bool(self._configured_secret())

    def authorized(self, authorization: Optional[str]) -> bool:
        secret = self._configured_secret()
        if not secret or not authorization:
            return False
        expected = f"Bearer {secret}"
        return hmac.compare_digest(authorization, expected)


@dataclass
class FixedWindowRateLimiter:
    """Best-effort process-local rate limiting for state-changing endpoints."""

    now: Callable[[], float] = time.monotonic
    max_keys: int = 4096

    def __post_init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        current = self.now()
        if len(self._events) >= self.max_keys and key not in self._events:
            stale_cutoff = current - max(window_seconds, 60)
            for stale_key in list(self._events):
                bucket = self._events[stale_key]
                while bucket and bucket[0] <= stale_cutoff:
                    bucket.popleft()
                if not bucket:
                    self._events.pop(stale_key, None)
                if len(self._events) < self.max_keys:
                    break
            if len(self._events) >= self.max_keys:
                return False
        bucket = self._events[key]
        cutoff = current - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(current)
        return True


class RequestBodyTooLarge(ValueError):
    """Raised internally when an ASGI request exceeds the configured body cap."""


class BodySizeLimitMiddleware:
    """Reject oversized HTTP request bodies before FastAPI parses JSON.

    Header-only checks are insufficient because chunked requests may omit
    Content-Length. This middleware counts ASGI body chunks and sends a small
    413 JSON response before an endpoint or Pydantic model receives the body.
    """

    def __init__(self, app, max_bytes: int = 262_144) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    await self._send_413(send)
                    return
            except ValueError:
                await self._send_413(send)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge("Request body exceeded the configured limit.")
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not response_started:
                await self._send_413(send)

    async def _send_413(self, send) -> None:
        payload = (
            b'{"ok":false,"error":{"code":"request_too_large",'
            b'"message":"Request body exceeds the allowed size.","retryable":false}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
