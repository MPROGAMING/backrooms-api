"""
BackroomsGPT Omni-Lore Gateway
===============================

Production-oriented FastAPI gateway for live Backrooms lore retrieval.

Design goals
------------
1. Query live sources first.
2. Keep canons separate and preserve provenance.
3. Distinguish "not found" from "source unavailable".
4. Support MediaWiki/Fandom and Wikidot through dedicated adapters.
5. Support dead or intermittently unavailable historical sources through
   explicit Internet Archive fallback, never silently pretending archived
   content is live.
6. Give Custom GPT Actions a small set of high-value operations:
   search, resolve, fetch, compare, recent changes, source listing, URL reader.
7. Keep legacy endpoints compatible with the previous BackroomsGPT schema.
8. Avoid hallucinated "official" classifications. Any heuristic analysis is
   labeled as heuristic and source text remains the authority.
9. Make the source registry extensible without rewriting routing logic.
10. Prevent arbitrary server-side request forgery by restricting dynamic URL
    reads to supported wiki hosts and configured source domains.

The gateway intentionally does not store a full scraped copy of the wikis.
It fetches live content on demand and uses a bounded in-memory TTL cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import math
import os
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from security import (
    BodySizeLimitMiddleware,
    FixedWindowRateLimiter,
    URLSafetyError,
    normalize_allowed_http_url,
    validate_discovery_target,
)


# =============================================================================
# 0. BUILD METADATA
# =============================================================================

APP_NAME = "BackroomsGPT Omni-Lore Gateway"
APP_VERSION = "21.0.0"
BUILD_NAME = "BACKROOMSGPT-FINAL-21"
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "18"))
DEFAULT_CONNECT_TIMEOUT_SECONDS = float(os.getenv("HTTP_CONNECT_TIMEOUT_SECONDS", "7"))
DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))
DEFAULT_NEGATIVE_CACHE_TTL_SECONDS = int(os.getenv("NEGATIVE_CACHE_TTL_SECONDS", "90"))
DEFAULT_MAX_CACHE_ITEMS = int(os.getenv("MAX_CACHE_ITEMS", "2500"))
DEFAULT_MAX_TEXT_CHARS = int(os.getenv("DEFAULT_MAX_TEXT_CHARS", "90000"))
ABSOLUTE_MAX_TEXT_CHARS = int(os.getenv("ABSOLUTE_MAX_TEXT_CHARS", "250000"))
# A Custom GPT Action needs concise, tool-safe responses.  Adapters may use a
# larger bounded internal budget for indexing, but public endpoints default to
# this smaller payload budget when a caller omits max_chars.
ACTION_DEFAULT_MAX_TEXT_CHARS = int(os.getenv("ACTION_DEFAULT_MAX_TEXT_CHARS", "24000"))
ACTION_ABSOLUTE_MAX_TEXT_CHARS = int(os.getenv("ACTION_ABSOLUTE_MAX_TEXT_CHARS", "60000"))
MAX_UPSTREAM_RESPONSE_BYTES = int(os.getenv("MAX_UPSTREAM_RESPONSE_BYTES", "4_000_000"))
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", "262_144"))
MAX_OMNI_CONCURRENCY = int(os.getenv("MAX_OMNI_CONCURRENCY", "10"))
MAX_SOURCE_SEARCH_RESULTS = int(os.getenv("MAX_SOURCE_SEARCH_RESULTS", "12"))
MAX_COMPARE_SOURCES = int(os.getenv("MAX_COMPARE_SOURCES", "12"))
MAX_CDX_ROWS = int(os.getenv("MAX_CDX_ROWS", "500"))
MAX_WIKIDOT_URL_CANDIDATES = int(os.getenv("MAX_WIKIDOT_URL_CANDIDATES", "24"))
MAX_ARCHIVE_AVAILABILITY_CHECKS = int(os.getenv("MAX_ARCHIVE_AVAILABILITY_CHECKS", "12"))
ARCHIVE_MIN_SIMILARITY = float(os.getenv("ARCHIVE_MIN_SIMILARITY", "0.78"))
LIVE_SEARCH_MIN_SIMILARITY = float(os.getenv("LIVE_SEARCH_MIN_SIMILARITY", "0.40"))
COMPARE_SECTION_EXCERPT_CHARS = int(os.getenv("COMPARE_SECTION_EXCERPT_CHARS", "900"))
MAX_QUERY_CHARS = int(os.getenv("MAX_QUERY_CHARS", "500"))
MAX_SOURCE_IDS_PER_REQUEST = int(os.getenv("MAX_SOURCE_IDS_PER_REQUEST", "12"))
PUBLIC_OUTBOUND_REQUESTS_PER_MINUTE = int(os.getenv("PUBLIC_OUTBOUND_REQUESTS_PER_MINUTE", "30"))
PUBLIC_OUTBOUND_GLOBAL_PER_MINUTE = int(os.getenv("PUBLIC_OUTBOUND_GLOBAL_PER_MINUTE", "240"))
SERVICE_STARTED_AT = time.monotonic()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(BUILD_NAME)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str, size: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:size]


def compact_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_query(value: str) -> str:
    value = unquote((value or "").strip())
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    value = value.strip()
    if len(value) > MAX_QUERY_CHARS:
        raise BadSourceQuery(f"Query must not exceed {MAX_QUERY_CHARS} characters.")
    return value


def normalize_title_key(value: str) -> str:
    value = normalize_query(value).casefold()
    value = re.sub(r"[^a-z0-9\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF]+", " ", value)
    return compact_ws(value)


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def action_text_budget(value: int) -> int:
    """Clamp public page-response text without constraining internal Atlas work."""
    return clamp_int(value, 2_000, ACTION_ABSOLUTE_MAX_TEXT_CHARS)


# =============================================================================
# 1. DOMAIN ERRORS
# =============================================================================

class GatewayError(Exception):
    """Base gateway error."""


class SourceNotFound(GatewayError):
    """The requested source ID does not exist in the registry."""


class PageNotFound(GatewayError):
    """A source was reachable, but the requested page could not be resolved."""


class SourceUnavailable(GatewayError):
    """The source could not be reached or returned an upstream failure."""


class UnsafeTarget(GatewayError):
    """A dynamic target URL or host is not allowed."""


class BadSourceQuery(GatewayError):
    """The source/query combination is invalid."""


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    source_id: Optional[str] = None,
    diagnostics: Optional[str] = None,
    retryable: bool = False,
    details: Optional[dict] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    payload: Dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
        "meta": {
            "service": APP_NAME,
            "version": APP_VERSION,
            "timestamp": utc_now_iso(),
        },
    }
    if source_id:
        payload["error"]["source_id"] = source_id
    if diagnostics:
        payload["error"]["diagnostics"] = diagnostics[:2000]
    if details:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=payload, headers=dict(headers or {}))


# =============================================================================
# 2. ASYNC TTL CACHE
# =============================================================================

@dataclass
class CacheEntry:
    data: Any
    expires_at: float
    created_at: float
    last_access: float
    hits: int = 0
    negative: bool = False


class AsyncTTLCache:
    """
    Bounded in-memory TTL cache.

    Render free instances may restart at any time, so the cache is treated as
    an optimization, never as durable storage.
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        negative_ttl_seconds: int = DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
        max_items: int = DEFAULT_MAX_CACHE_ITEMS,
    ):
        self.ttl_seconds = ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds
        self.max_items = max_items
        self._data: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        now = time.monotonic()
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._data.pop(key, None)
                return None
            entry.hits += 1
            entry.last_access = now
            return entry.data

    async def set(
        self,
        key: str,
        data: Any,
        *,
        ttl_seconds: Optional[int] = None,
        negative: bool = False,
    ) -> None:
        now = time.monotonic()
        ttl = ttl_seconds
        if ttl is None:
            ttl = self.negative_ttl_seconds if negative else self.ttl_seconds

        async with self._lock:
            if len(self._data) >= self.max_items:
                await self._evict_locked()
            self._data[key] = CacheEntry(
                data=data,
                expires_at=now + ttl,
                created_at=now,
                last_access=now,
                hits=0,
                negative=negative,
            )

    async def _evict_locked(self) -> None:
        if not self._data:
            return
        victim_key = min(
            self._data,
            key=lambda k: (
                self._data[k].hits,
                self._data[k].last_access,
                self._data[k].created_at,
            ),
        )
        self._data.pop(victim_key, None)

    async def purge_expired(self) -> int:
        now = time.monotonic()
        async with self._lock:
            expired = [k for k, v in self._data.items() if v.expires_at <= now]
            for key in expired:
                self._data.pop(key, None)
            return len(expired)

    async def stats(self) -> dict:
        now = time.monotonic()
        async with self._lock:
            alive = [v for v in self._data.values() if v.expires_at > now]
            return {
                "items": len(alive),
                "max_items": self.max_items,
                "positive_items": sum(1 for v in alive if not v.negative),
                "negative_items": sum(1 for v in alive if v.negative),
                "total_hits": sum(v.hits for v in alive),
            }


cache = AsyncTTLCache()


# =============================================================================
# 3. NETWORK RESILIENCE
# =============================================================================

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 45.0
    failures: int = 0
    last_failure_at: float = 0.0
    state: CircuitState = CircuitState.CLOSED

    def can_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure_at >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_at = time.monotonic()
        if self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN


@dataclass
class FetchDiagnostic:
    url: str
    ok: bool
    status_code: Optional[int]
    elapsed_ms: int
    error_type: Optional[str] = None
    message: Optional[str] = None


@dataclass
class LimitedFetchResponse:
    """A bounded response used only for untrusted Source Discovery seeds."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    diagnostics: List[FetchDiagnostic]

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class ResilientHTTPClient:
    """
    Shared async HTTP client with:
    - bounded connections
    - per-host concurrency
    - retries with exponential backoff + jitter
    - host circuit breakers
    - explicit 404 handling
    - diagnostics for upstream failures
    """

    def __init__(self) -> None:
        timeout = httpx.Timeout(
            DEFAULT_TIMEOUT_SECONDS,
            connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=80,
            max_keepalive_connections=24,
            keepalive_expiry=30.0,
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            # Redirects are followed manually below so each destination goes
            # through the same allowlist/DNS validation as the initial URL.
            follow_redirects=False,
            trust_env=False,
            limits=limits,
        )
        self.breakers: Dict[str, CircuitBreaker] = defaultdict(CircuitBreaker)
        self.host_semaphores: Dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(6)
        )
        self.user_agents = [
            "BackroomsGPT-LoreGateway/21.0 (+https://backrooms-api.onrender.com/privacy)",
            "M.E.G.-Archive-Resolver/21.0",
            "Mozilla/5.0 (compatible; BackroomsGPT/21.0; lore-retrieval)",
        ]
        self._target_validator: Optional[Callable[[str], Awaitable[str]]] = None

    def set_target_validator(self, validator: Callable[[str], Awaitable[str]]) -> None:
        """Install the central allowlist + DNS validator after registry setup."""
        self._target_validator = validator

    async def _validate_target(self, url: str) -> str:
        if self._target_validator is None:
            return url
        return await self._target_validator(url)

    def _headers(self, *, json_preferred: bool = False) -> dict:
        accept = "application/json,text/plain;q=0.9,*/*;q=0.7" if json_preferred else (
            "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7"
        )
        return {
            "User-Agent": random.choice(self.user_agents),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.8",
            "Cache-Control": "no-cache",
        }

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        retries: int = 2,
        json_preferred: bool = False,
        extra_headers: Optional[Mapping[str, str]] = None,
        allow_404: bool = True,
        max_response_bytes: int = MAX_UPSTREAM_RESPONSE_BYTES,
        max_redirects: int = 3,
    ) -> Tuple[Optional[httpx.Response], List[FetchDiagnostic]]:
        headers = self._headers(json_preferred=json_preferred)
        if extra_headers:
            headers.update(extra_headers)

        diagnostics: List[FetchDiagnostic] = []
        initial_url = await self._validate_target(url)

        for attempt in range(retries + 1):
            current_url = initial_url
            current_params = params
            redirects = 0
            try:
                while True:
                    host = urlparse(current_url).hostname or ""
                    breaker = self.breakers[host]
                    if not breaker.can_request():
                        raise SourceUnavailable(f"Circuit breaker open for upstream host {host}")

                    started = time.monotonic()
                    async with self.host_semaphores[host]:
                        async with self.client.stream(
                            method,
                            current_url,
                            params=current_params,
                            headers=headers,
                            follow_redirects=False,
                        ) as upstream:
                            elapsed_ms = int((time.monotonic() - started) * 1000)
                            status = upstream.status_code
                            response_url = str(upstream.url)

                            if 300 <= status < 400:
                                location = upstream.headers.get("location")
                                if not location:
                                    raise SourceUnavailable(
                                        f"Upstream {host} returned a redirect without a Location header"
                                    )
                                if redirects >= max_redirects:
                                    raise SourceUnavailable(
                                        f"Upstream redirect limit exceeded for {url}"
                                    )
                                next_url = urljoin(response_url, location)
                                current_url = await self._validate_target(next_url)
                                current_params = None
                                redirects += 1
                                diagnostics.append(
                                    FetchDiagnostic(
                                        url=response_url,
                                        ok=True,
                                        status_code=status,
                                        elapsed_ms=elapsed_ms,
                                        error_type="redirect",
                                        message="Validated redirect followed manually",
                                    )
                                )
                                continue

                            declared_length = upstream.headers.get("content-length")
                            if declared_length:
                                try:
                                    if int(declared_length) > max_response_bytes:
                                        raise SourceUnavailable(
                                            f"Upstream response exceeded {max_response_bytes} bytes"
                                        )
                                except ValueError:
                                    pass

                            chunks: List[bytes] = []
                            total = 0
                            async for chunk in upstream.aiter_bytes():
                                total += len(chunk)
                                if total > max_response_bytes:
                                    raise SourceUnavailable(
                                        f"Upstream response exceeded {max_response_bytes} bytes"
                                    )
                                chunks.append(chunk)
                            response = httpx.Response(
                                status,
                                headers=upstream.headers,
                                content=b"".join(chunks),
                                request=upstream.request,
                            )

                    if response.status_code == 404 and allow_404:
                        diagnostics.append(
                            FetchDiagnostic(
                                url=str(response.url),
                                ok=False,
                                status_code=404,
                                elapsed_ms=elapsed_ms,
                                error_type="not_found",
                                message="Upstream returned 404",
                            )
                        )
                        breaker.record_success()
                        return response, diagnostics

                    if 200 <= response.status_code < 300:
                        diagnostics.append(
                            FetchDiagnostic(
                                url=str(response.url),
                                ok=True,
                                status_code=response.status_code,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                        breaker.record_success()
                        return response, diagnostics

                    diagnostics.append(
                        FetchDiagnostic(
                            url=str(response.url),
                            ok=False,
                            status_code=response.status_code,
                            elapsed_ms=elapsed_ms,
                            error_type="upstream_http",
                            message=f"HTTP {response.status_code}",
                        )
                    )
                    retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
                    if not retryable or attempt >= retries:
                        breaker.record_failure()
                        raise SourceUnavailable(
                            f"Upstream {host} returned HTTP {response.status_code}"
                        )
                    break
            except (SourceUnavailable, UnsafeTarget):
                raise
            except httpx.HTTPError as exc:
                host = urlparse(current_url).hostname or "unknown"
                elapsed_ms = int((time.monotonic() - started) * 1000)
                diagnostics.append(
                    FetchDiagnostic(
                        url=current_url,
                        ok=False,
                        status_code=None,
                        elapsed_ms=elapsed_ms,
                        error_type=type(exc).__name__,
                        message=str(exc)[:500],
                    )
                )
                if attempt >= retries:
                    self.breakers[host].record_failure()
                    raise SourceUnavailable(
                        f"Unable to reach upstream host {host}: {type(exc).__name__}"
                    ) from exc

            delay = min(4.0, (0.35 * (2 ** attempt)) + random.uniform(0.0, 0.25))
            await asyncio.sleep(delay)

        raise SourceUnavailable(f"Exhausted retries for {url}")

    async def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        retries: int = 2,
        json_preferred: bool = False,
        allow_404: bool = True,
    ) -> Tuple[Optional[httpx.Response], List[FetchDiagnostic]]:
        return await self.request(
            "GET",
            url,
            params=params,
            retries=retries,
            json_preferred=json_preferred,
            allow_404=allow_404,
        )

    async def get_limited(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float = 8.0,
    ) -> LimitedFetchResponse:
        """Fetch a small untrusted HTML document without following redirects.

        Source Discovery validates each redirect destination itself.  Streaming
        keeps an oversized response from being buffered in memory.
        """
        url = await self._validate_target(url)
        parsed = urlparse(url)
        host = parsed.hostname or ""
        breaker = self.breakers[host]
        if not breaker.can_request():
            raise SourceUnavailable(f"Circuit breaker open for upstream host {host}")

        timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(DEFAULT_CONNECT_TIMEOUT_SECONDS, timeout_seconds),
        )
        started = time.monotonic()
        try:
            async with self.host_semaphores[host]:
                async with self.client.stream(
                    "GET",
                    url,
                    headers=self._headers(),
                    follow_redirects=False,
                    timeout=timeout,
                ) as response:
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    diagnostic = FetchDiagnostic(
                        url=str(response.url),
                        ok=200 <= response.status_code < 400,
                        status_code=response.status_code,
                        elapsed_ms=elapsed_ms,
                        error_type=None if 200 <= response.status_code < 400 else "upstream_http",
                        message=None if 200 <= response.status_code < 400 else f"HTTP {response.status_code}",
                    )
                    if 300 <= response.status_code < 400:
                        breaker.record_success()
                        return LimitedFetchResponse(
                            url=str(response.url),
                            status_code=response.status_code,
                            headers=dict(response.headers),
                            content=b"",
                            diagnostics=[diagnostic],
                        )

                    declared_length = response.headers.get("content-length")
                    if declared_length:
                        try:
                            if int(declared_length) > max_bytes:
                                raise SourceUnavailable("Discovery response exceeded the configured size limit.")
                        except ValueError:
                            pass

                    chunks: List[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise SourceUnavailable("Discovery response exceeded the configured size limit.")
                        chunks.append(chunk)
                    breaker.record_success()
                    return LimitedFetchResponse(
                        url=str(response.url),
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        content=b"".join(chunks),
                        diagnostics=[diagnostic],
                    )
        except SourceUnavailable:
            raise
        except httpx.HTTPError as exc:
            breaker.record_failure()
            raise SourceUnavailable(
                f"Unable to reach upstream host {host}: {type(exc).__name__}"
            ) from exc

    async def close(self) -> None:
        await self.client.aclose()


network = ResilientHTTPClient()


# =============================================================================
# 4. SOURCE REGISTRY
# =============================================================================

class SourceKind(str, Enum):
    MEDIAWIKI = "mediawiki"
    WIKIDOT = "wikidot"
    WEB = "web"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class SourceConfig:
    id: str
    name: str
    kind: SourceKind
    canon: str
    priority: int
    language: str = "en"
    live: bool = True
    searchable: bool = True
    recent_changes: bool = True
    api_url: Optional[str] = None
    base_urls: Tuple[str, ...] = field(default_factory=tuple)
    page_url_template: Optional[str] = None
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    tags: Tuple[str, ...] = field(default_factory=tuple)
    archive_domains: Tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def public_dict(self) -> dict:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["base_urls"] = list(self.base_urls)
        data["aliases"] = list(self.aliases)
        data["tags"] = list(self.tags)
        data["archive_domains"] = list(self.archive_domains)
        return data


class SourceRegistry:
    def __init__(self, sources: Sequence[SourceConfig]):
        self._sources: Dict[str, SourceConfig] = {s.id: s for s in sources}
        self._alias_map: Dict[str, str] = {}
        for source in sources:
            self._alias_map[source.id.casefold()] = source.id
            self._alias_map[source.name.casefold()] = source.id
            for alias in source.aliases:
                self._alias_map[alias.casefold()] = source.id

    def get(self, source_id_or_alias: str) -> SourceConfig:
        key = (source_id_or_alias or "").strip()
        direct = self._sources.get(key)
        if direct:
            return direct
        resolved_id = self._alias_map.get(key.casefold())
        if resolved_id:
            return self._sources[resolved_id]
        raise SourceNotFound(f"Unknown source '{source_id_or_alias}'")

    def list(
        self,
        *,
        canon: Optional[str] = None,
        kind: Optional[str] = None,
        language: Optional[str] = None,
        live_only: bool = False,
        tag: Optional[str] = None,
    ) -> List[SourceConfig]:
        items = list(self._sources.values())
        if canon:
            items = [s for s in items if s.canon.casefold() == canon.casefold()]
        if kind:
            items = [s for s in items if s.kind.value == kind.casefold()]
        if language:
            items = [s for s in items if s.language.casefold() == language.casefold()]
        if live_only:
            items = [s for s in items if s.live]
        if tag:
            items = [s for s in items if tag.casefold() in {t.casefold() for t in s.tags}]
        return sorted(items, key=lambda s: (s.priority, s.name.casefold()))

    def all_ids(self) -> List[str]:
        return sorted(self._sources)


BUILTIN_SOURCES: List[SourceConfig] = [
    SourceConfig(
        id="fandom-main",
        name="Backrooms Wiki (Fandom)",
        kind=SourceKind.MEDIAWIKI,
        canon="Fandom",
        priority=10,
        api_url="https://backrooms.fandom.com/api.php",
        page_url_template="https://backrooms.fandom.com/wiki/{title}",
        aliases=("fandom", "backrooms-fandom", "main-fandom"),
        tags=("core", "community", "fandom"),
        notes="Main English Backrooms Fandom community.",
    ),
    SourceConfig(
        id="wikidot-main",
        name="Backrooms Wiki (Wikidot)",
        kind=SourceKind.WIKIDOT,
        canon="Wikidot",
        priority=10,
        base_urls=("https://backrooms-wiki.wikidot.com",),
        aliases=("wikidot", "backrooms-wikidot", "main-wikidot"),
        tags=("core", "community", "wikidot"),
        notes="Main English Backrooms Wikidot continuity.",
    ),
    SourceConfig(
        id="liminal-archives",
        name="Liminal Archives",
        kind=SourceKind.WIKIDOT,
        canon="Liminal Archives",
        priority=20,
        base_urls=(
            "https://liminal-archives.wikidot.com",
            "https://liminalarchives.wikidot.com",
        ),
        aliases=("liminal", "la", "liminalarchives"),
        tags=("alternative", "community", "wikidot", "historical"),
        archive_domains=("liminal-archives.wikidot.com", "liminalarchives.wikidot.com", "liminalarchives.xyz"),
        notes="Independent continuity. Live Wikidot resolution first; explicit Wayback fallback is available.",
    ),
    SourceConfig(
        id="freewriting-fandom",
        name="Backrooms Freewriting Wiki",
        kind=SourceKind.MEDIAWIKI,
        canon="Freewriting",
        priority=30,
        api_url="https://backrooms-freewriting.fandom.com/api.php",
        page_url_template="https://backrooms-freewriting.fandom.com/wiki/{title}",
        aliases=("freewriting", "free-writing"),
        tags=("alternative", "community", "fandom"),
    ),
    SourceConfig(
        id="kanepixels-fandom",
        name="Kane Pixels Backrooms Wiki",
        kind=SourceKind.MEDIAWIKI,
        canon="Kane Pixels Community Documentation",
        priority=25,
        api_url="https://kane-pixels-backrooms.fandom.com/api.php",
        page_url_template="https://kane-pixels-backrooms.fandom.com/wiki/{title}",
        aliases=("kane", "kanepixels", "kane-pixels"),
        tags=("cinematic", "community-documentation", "fandom"),
        notes="Community-maintained documentation about the Kane Pixels continuity; not a substitute for primary videos.",
    ),
]


# International Wikidot candidates.
#
# These are registry candidates, not a claim that every host is currently live.
# The health endpoint reports actual reachability. Generic Wikidot endpoints also
# let the GPT read any supported *.wikidot.com site by explicit site hostname.
INTERNATIONAL_WIKIDOT_CANDIDATES: List[Tuple[str, str, str, Tuple[str, ...]]] = [
    ("wikidot-ru", "Backrooms Wikidot Russian Branch", "ru", ("https://backrooms-ru.wikidot.com",)),
    ("wikidot-cn", "Backrooms Wikidot Chinese Branch", "zh", ("https://backrooms-cn.wikidot.com",)),
    ("wikidot-es", "Backrooms Wikidot Spanish Branch", "es", ("https://backrooms-es.wikidot.com",)),
    ("wikidot-fr", "Backrooms Wikidot French Branch", "fr", ("https://backrooms-fr.wikidot.com",)),
    ("wikidot-de", "Backrooms Wikidot German Branch", "de", ("https://backrooms-de.wikidot.com",)),
    ("wikidot-it", "Backrooms Wikidot Italian Branch", "it", ("https://backrooms-it.wikidot.com",)),
    ("wikidot-pl", "Backrooms Wikidot Polish Branch", "pl", ("https://backrooms-pl.wikidot.com",)),
    ("wikidot-ptbr", "Backrooms Wikidot Portuguese Branch Candidate", "pt-BR", ("https://backrooms-pt-br.wikidot.com",)),
    ("wikidot-jp", "Backrooms Wikidot Japanese Branch Candidate", "ja", ("https://backrooms-jp.wikidot.com",)),
    ("wikidot-ko", "Backrooms Wikidot Korean Branch Candidate", "ko", ("https://backrooms-ko.wikidot.com",)),
]

for source_id, name, language, bases in INTERNATIONAL_WIKIDOT_CANDIDATES:
    BUILTIN_SOURCES.append(
        SourceConfig(
            id=source_id,
            name=name,
            kind=SourceKind.WIKIDOT,
            canon=f"Wikidot International ({language})",
            priority=50,
            language=language,
            base_urls=bases,
            aliases=(language, f"intl-{language}"),
            tags=("international", "wikidot", "candidate"),
            notes="Host is probed at runtime. Use /sources/health to confirm current reachability.",
        )
    )

registry = SourceRegistry(BUILTIN_SOURCES)


# =============================================================================
# 5. URL SAFETY
# =============================================================================

ALLOWED_DYNAMIC_SUFFIXES = (
    ".fandom.com",
    ".wikidot.com",
)

EXPLICIT_ALLOWED_HOSTS: Set[str] = {
    "backrooms.fandom.com",
    "backrooms-freewriting.fandom.com",
    "kane-pixels-backrooms.fandom.com",
    "web.archive.org",
    "archive.org",
    "commons.wikimedia.org",
}

for _source in BUILTIN_SOURCES:
    if _source.api_url:
        EXPLICIT_ALLOWED_HOSTS.add(urlparse(_source.api_url).hostname or "")
    for _base in _source.base_urls:
        EXPLICIT_ALLOWED_HOSTS.add(urlparse(_base).hostname or "")
    if _source.page_url_template:
        EXPLICIT_ALLOWED_HOSTS.add(urlparse(_source.page_url_template).hostname or "")


def validate_public_http_url(url: str) -> str:
    try:
        normalized = normalize_allowed_http_url(
            url,
            explicit_hosts=EXPLICIT_ALLOWED_HOSTS,
            allowed_suffixes=ALLOWED_DYNAMIC_SUFFIXES,
        )
        if urlparse(normalized).scheme != "https":
            raise URLSafetyError("Only HTTPS targets are supported by this service.")
        return normalized
    except URLSafetyError as exc:
        raise UnsafeTarget(str(exc)) from exc


async def validate_discovery_seed_url(url: str) -> str:
    """Validate Source Discovery seeds before every initial/redirect fetch.

    The feature is intentionally constrained to the same public Fandom and
    Wikidot platform suffixes used by dynamic retrieval.  It preflights all DNS
    answers, but does not claim generic DNS-rebinding protection because Python's
    HTTP client is not pinned to a resolved address.  Restricting the feature to
    those platform-owned suffixes is the security boundary.
    """
    try:
        normalized = await validate_discovery_target(
            url,
            explicit_hosts=(),
            allowed_suffixes=ALLOWED_DYNAMIC_SUFFIXES,
        )
        if urlparse(normalized).scheme != "https":
            raise URLSafetyError("Source Discovery only accepts HTTPS seed URLs.")
        return normalized
    except URLSafetyError as exc:
        raise UnsafeTarget(str(exc)) from exc


async def validate_server_fetch_target(url: str) -> str:
    """Central URL policy for every server-side network request.

    The HTTP client calls this before its initial request and again before every
    redirect. It combines the established source allowlist with DNS validation.
    Connections are intentionally not claimed to be DNS-pinned; the defensible
    boundary is restricted platform/configured hosts plus preflight validation.
    """
    try:
        normalized = await validate_discovery_target(
            url,
            explicit_hosts=EXPLICIT_ALLOWED_HOSTS,
            allowed_suffixes=ALLOWED_DYNAMIC_SUFFIXES,
        )
        if urlparse(normalized).scheme != "https":
            raise URLSafetyError("Only HTTPS upstream targets are supported.")
        return normalized
    except URLSafetyError as exc:
        raise UnsafeTarget(str(exc)) from exc


network.set_target_validator(validate_server_fetch_target)


def validate_dynamic_site_hostname(site: str, expected_suffix: str) -> str:
    raw = (site or "").strip().casefold()
    if not raw:
        raise BadSourceQuery("A site hostname or subdomain is required.")

    if "://" in raw:
        host = urlparse(raw).hostname or ""
    else:
        host = raw

    if "." not in host:
        host = f"{host}{expected_suffix}"

    if not host.endswith(expected_suffix):
        raise UnsafeTarget(f"Dynamic site must end with '{expected_suffix}'.")

    validate_public_http_url(f"https://{host}/")
    return host


# =============================================================================
# 6. HTML SANITIZATION AND PAGE ANALYSIS
# =============================================================================

NOISE_SELECTORS = [
    "script",
    "style",
    "noscript",
    "iframe",
    "nav",
    "footer",
    "form",
    ".page-rate-widget-box",
    ".page-tags",
    ".footer-wikiwiki",
    ".wd-adunit",
    ".toc",
    ".printuser",
    ".license-area",
    ".wds-global-navigation-wrapper",
    ".global-navigation",
    ".fandom-sticky-header",
    ".page__right-rail",
    ".rail-module",
    ".mcf-wrapper",
    "#WikiaBar",
    "#WikiaRail",
]

MISSING_PAGE_MARKERS = (
    "the page you want to access does not exist",
    "the page you are looking for does not exist",
    "this page doesn't exist",
    "there is currently no text in this page",
    "create a page",
)

HEADING_CANONICAL_MAP = {
    "description": "description",
    "summary": "description",
    "bases outposts and communities": "settlements",
    "bases outposts communities": "settlements",
    "colonies and outposts": "settlements",
    "communities": "settlements",
    "entities": "entities",
    "entrances and exits": "transitions",
    "entrances exits": "transitions",
    "entrances": "entrances",
    "exits": "exits",
    "discovery": "discovery",
    "addendum": "addendum",
    "phenomena": "phenomena",
    "objects": "objects",
}


@dataclass
class SanitizedDocument:
    title: str
    text: str
    headings: List[str]
    sections: Dict[str, str]
    links: List[dict]
    image_urls: List[str]
    meta_description: Optional[str]
    language_hint: Optional[str]
    missing_page_signal: bool
    character_count: int


def _best_content_container(soup: BeautifulSoup, preferred_id: Optional[str] = None):
    candidates = []
    if preferred_id:
        candidates.append(soup.find(id=preferred_id))
    candidates.extend(
        [
            soup.find(id="page-content"),
            soup.find(id="mw-content-text"),
            soup.find("main"),
            soup.find("article"),
            soup.body,
            soup,
        ]
    )
    return next((candidate for candidate in candidates if candidate is not None), soup)


def sanitize_html_document(
    html_text: str,
    *,
    page_url: str,
    preferred_container_id: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> SanitizedDocument:
    max_chars = clamp_int(max_chars, 2000, ABSOLUTE_MAX_TEXT_CHARS)
    soup = BeautifulSoup(html_text or "", "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = compact_ws(h1.get_text(" ", strip=True))
    if not title and soup.title:
        title = compact_ws(soup.title.get_text(" ", strip=True))
    title = re.sub(r"\s*[-|]\s*(Fandom|Wikidot).*$", "", title, flags=re.IGNORECASE).strip()

    meta_description = None
    meta_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if meta_tag and meta_tag.get("content"):
        meta_description = compact_ws(meta_tag.get("content"))

    language_hint = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        language_hint = html_tag.get("lang")

    content = _best_content_container(soup, preferred_container_id)

    # Remove page chrome before extracting links/sections. Navigation links are
    # not article relationships and otherwise pollute the Atlas graph.
    for selector in NOISE_SELECTORS:
        for node in content.select(selector):
            node.decompose()

    links: List[dict] = []
    seen_links: Set[Tuple[str, str]] = set()
    for anchor in content.find_all("a", href=True):
        href = urljoin(page_url, anchor.get("href"))
        label = compact_ws(anchor.get_text(" ", strip=True))
        if not href.startswith(("http://", "https://")):
            continue
        key = (label.casefold(), href)
        if key in seen_links:
            continue
        seen_links.add(key)
        links.append({"title": label or href.rsplit("/", 1)[-1], "url": href})
        if len(links) >= 500:
            break

    image_urls: List[str] = []
    seen_images: Set[str] = set()
    for img in content.find_all("img"):
        src = img.get("data-src") or img.get("src")
        if not src:
            continue
        absolute = urljoin(page_url, src)
        if absolute in seen_images:
            continue
        seen_images.add(absolute)
        image_urls.append(absolute)
        if len(image_urls) >= 100:
            break

    headings: List[str] = []
    section_map: Dict[str, str] = {}
    current_heading = "__intro__"
    section_buffers: Dict[str, List[str]] = defaultdict(list)

    # Build section text in DOM order.
    for node in content.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"]):
        if node.name in {"h1", "h2", "h3", "h4"}:
            heading = compact_ws(node.get_text(" ", strip=True))
            if heading:
                headings.append(heading)
                current_heading = heading
            continue
        text = compact_ws(node.get_text(" ", strip=True))
        if text:
            section_buffers[current_heading].append(text)

    clean_text = content.get_text(separator="\n", strip=True)
    clean_text = html.unescape(clean_text)
    clean_text = re.sub(r"[ \t]+\n", "\n", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
    clean_text = clean_text.strip()

    for heading, buffer in section_buffers.items():
        section_text = "\n".join(buffer).strip()
        if section_text:
            section_map[heading] = section_text[:40000]

    lowered = clean_text.casefold()
    missing_signal = (
        len(clean_text) < 400
        and any(marker in lowered for marker in MISSING_PAGE_MARKERS)
    )

    return SanitizedDocument(
        title=title,
        text=clean_text[:max_chars],
        headings=headings[:100],
        sections=section_map,
        links=links,
        image_urls=image_urls,
        meta_description=meta_description,
        language_hint=language_hint,
        missing_page_signal=missing_signal,
        character_count=len(clean_text),
    )


class LoreAnalyzer:
    """
    Conservative local extractive analysis.

    This module does NOT claim to infer official Survival Difficulty classes.
    It extracts visible structure, section signals, repeated named terms, and
    an explicitly heuristic hazard signal useful for retrieval/ranking.
    """

    STOPWORDS = {
        "the", "and", "that", "this", "with", "from", "into", "have", "has",
        "was", "were", "are", "for", "you", "your", "they", "their", "them",
        "but", "not", "can", "cannot", "will", "would", "there", "here", "level",
        "entity", "entities", "backrooms", "page", "wiki", "also", "been", "being",
        "which", "when", "where", "what", "who", "how", "its", "than", "then",
    }

    HAZARD_TERMS = {
        "safe": -2,
        "secure": -2,
        "stable": -1,
        "habitable": -2,
        "peaceful": -2,
        "danger": 2,
        "dangerous": 3,
        "unsafe": 2,
        "unstable": 2,
        "hostile": 3,
        "lethal": 5,
        "deadly": 4,
        "death": 4,
        "toxic": 3,
        "hazard": 2,
        "infestation": 4,
        "uninhabitable": 3,
    }

    SECTION_ALIASES = {
        "description": ("description", "summary", "overview"),
        "entities": ("entities", "entity"),
        "settlements": (
            "bases outposts and communities",
            "bases outposts communities",
            "colonies and outposts",
            "communities",
            "outposts",
        ),
        "entrances": ("entrances", "entry", "how to enter"),
        "exits": ("exits", "how to leave"),
        "transitions": ("entrances and exits", "entrances exits"),
        "discovery": ("discovery", "discovery log"),
    }

    @classmethod
    def tokenize(cls, text: str) -> List[str]:
        words = re.findall(r"\b[\w'-]{3,}\b", (text or "").casefold(), flags=re.UNICODE)
        return [w for w in words if w not in cls.STOPWORDS and not w.isdigit()]

    @classmethod
    def heuristic_hazard_signal(cls, text: str) -> dict:
        tokens = cls.tokenize(text)
        counts = Counter(tokens)
        raw_score = sum(counts[term] * weight for term, weight in cls.HAZARD_TERMS.items())
        normalized = 0.0
        if tokens:
            normalized = raw_score / math.sqrt(len(tokens))
        if normalized <= -0.8:
            label = "low-hazard language"
        elif normalized < 0.8:
            label = "mixed/neutral hazard language"
        elif normalized < 2.2:
            label = "elevated hazard language"
        else:
            label = "severe hazard language"
        evidence = [
            {"term": term, "count": counts[term], "weight": weight}
            for term, weight in cls.HAZARD_TERMS.items()
            if counts[term] > 0
        ]
        evidence.sort(key=lambda item: abs(item["count"] * item["weight"]), reverse=True)
        return {
            "label": label,
            "score": round(normalized, 3),
            "method": "heuristic lexical signal; not an official Survival Difficulty classification",
            "evidence": evidence[:12],
        }

    @classmethod
    def extractive_summary(cls, text: str, sentence_count: int = 5) -> str:
        text = compact_ws(text)
        if not text:
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        sentences = [s.strip() for s in sentences if 6 <= len(s.split()) <= 80]
        if len(sentences) <= sentence_count:
            return " ".join(sentences)

        global_counts = Counter(cls.tokenize(text))
        scored: List[Tuple[float, int, str]] = []
        for index, sentence in enumerate(sentences):
            tokens = cls.tokenize(sentence)
            if not tokens:
                continue
            lexical = sum(global_counts[t] for t in set(tokens)) / math.sqrt(len(tokens))
            position_bonus = 1.25 if index < 4 else 1.0
            length_penalty = 0.75 if len(tokens) > 55 else 1.0
            scored.append((lexical * position_bonus * length_penalty, index, sentence))

        selected = sorted(sorted(scored, reverse=True)[:sentence_count], key=lambda x: x[1])
        return " ".join(item[2] for item in selected)

    @classmethod
    def _match_section(cls, sections: Mapping[str, str], aliases: Sequence[str]) -> Optional[str]:
        for heading, value in sections.items():
            normalized = normalize_title_key(heading)
            if any(alias == normalized or alias in normalized for alias in aliases):
                return value
        return None

    @classmethod
    def extract_named_signals(cls, text: str) -> dict:
        patterns = {
            "level_designations": r"\bLevel\s+(?:[A-Za-z0-9_.:+-]+)\b",
            "entity_designations": r"\bEntity\s+(?:[A-Za-z0-9_.:+-]+)\b",
            "groups": r"\b(?:M\.E\.G\.|B\.N\.T\.G\.|U\.E\.C\.|Async|A-Sync)\b",
            "class_mentions": r"\bClass\s+(?:0|1|2|3|4|5|Deadzone|Habitable|Unknown|Undetermined)\b",
        }
        output: Dict[str, List[str]] = {}
        for key, pattern in patterns.items():
            found = list(dict.fromkeys(re.findall(pattern, text, flags=re.IGNORECASE)))
            output[key] = found[:40]
        return output

    @classmethod
    def analyze(cls, doc: SanitizedDocument) -> dict:
        canonical_sections: Dict[str, Optional[str]] = {}
        for key, aliases in cls.SECTION_ALIASES.items():
            canonical_sections[key] = cls._match_section(doc.sections, aliases)

        transitions = canonical_sections.get("transitions")
        entrances = canonical_sections.get("entrances")
        exits = canonical_sections.get("exits")
        if transitions and not entrances:
            entrances = transitions
        if transitions and not exits:
            exits = transitions

        return {
            "summary": cls.extractive_summary(doc.text, sentence_count=5),
            "heuristic_hazard_signal": cls.heuristic_hazard_signal(doc.text),
            "named_signals": cls.extract_named_signals(doc.text),
            "detected_sections": {
                key: bool(value) for key, value in canonical_sections.items()
            },
            "section_extracts": {
                "entities": (canonical_sections.get("entities") or "")[:6000],
                "settlements": (canonical_sections.get("settlements") or "")[:6000],
                "entrances": (entrances or "")[:6000],
                "exits": (exits or "")[:6000],
            },
            "document_stats": {
                "characters_before_truncation": doc.character_count,
                "returned_characters": len(doc.text),
                "heading_count": len(doc.headings),
                "link_count": len(doc.links),
                "image_count": len(doc.image_urls),
            },
        }


# =============================================================================
# 7. SLUG AND TITLE RESOLUTION
# =============================================================================

LEVEL_WORDS_BY_LANGUAGE = {
    "en": ("level",),
    "ru": ("level", "uroven", "уровень"),
    "zh": ("level", "层级", "樓層", "层"),
    "es": ("level", "nivel"),
    "fr": ("level", "niveau"),
    "de": ("level", "ebene"),
    "it": ("level", "livello"),
    "pl": ("level", "poziom"),
    "pt-BR": ("level", "nivel"),
    "ja": ("level", "レベル"),
    "ko": ("level", "레벨"),
}


def generate_slug_candidates(raw: str, *, language: str = "en") -> List[str]:
    """
    Build ordered candidate slugs without using a set, so resolution order is
    deterministic and testable.
    """
    base = normalize_query(raw)
    if not base:
        return []

    candidates: List[str] = []

    def add(value: str) -> None:
        value = value.strip().strip("/")
        value = re.sub(r"-{2,}", "-", value)
        if value and value not in candidates:
            candidates.append(value)

    add(base)
    add(base.replace(" ", "-"))
    add(base.replace(" ", "_"))
    add(base.casefold())
    add(base.casefold().replace(" ", "-"))
    add(base.casefold().replace(" ", "_"))
    add(re.sub(r"\s+", "", base.casefold()))

    # Preserve Wikidot system namespace syntax.
    for candidate in list(candidates):
        add(re.sub(r"^system[-_]", "system:", candidate, flags=re.IGNORECASE))
        add(re.sub(r"^component[-_]", "component:", candidate, flags=re.IGNORECASE))

    number_match = re.search(r"(?<!\d)(-?\d+(?:\.\d+)?)(?!\d)", base)
    if number_match:
        number = number_match.group(1)
        words = LEVEL_WORDS_BY_LANGUAGE.get(language, LEVEL_WORDS_BY_LANGUAGE["en"])
        for word in words:
            add(f"{word}-{number}")
            add(f"{word}_{number}")
            add(f"{word}{number}")
            add(f"{word} {number}")
        add(number)

    # Normalize punctuation commonly found in wiki slugs.
    for candidate in list(candidates):
        add(re.sub(r"[^0-9A-Za-z\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF:_-]", "-", candidate))
        add(re.sub(r"[^0-9A-Za-z\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF:_-]", "", candidate))

    return candidates[:40]


def title_similarity(query: str, title: str) -> float:
    q = normalize_title_key(query)
    t = normalize_title_key(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    containment = 0.92 if q in t or t in q else 0.0
    return max(containment, SequenceMatcher(None, q, t).ratio())


_GENERIC_MATCH_TOKENS = {
    "level", "entity", "object", "page", "wiki", "the", "a", "an",
    "backrooms", "archive", "archives", "liminal",
}


def extract_level_identifier(value: str) -> Optional[str]:
    """Extract the explicit Level number from a title, slug, or URL.

    The comparison is intentionally strict. A query for Level 0 must never
    accept Level 10.1 merely because the strings are superficially similar.
    """
    raw = unquote(value or "").casefold()
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"\s+", " ", raw)
    match = re.search(r"\blevel\s+(-?\d+(?:\.\d+)?)\b", raw)
    if not match:
        return None
    number = match.group(1)
    try:
        parsed = float(number)
        return str(int(parsed)) if parsed.is_integer() else str(parsed).rstrip("0").rstrip(".")
    except ValueError:
        return number


def archive_candidate_match_score(
    query: str,
    candidate_title: str,
    original_url: str = "",
) -> float:
    """Return a conservative identity score for an archived candidate.

    This scorer intentionally rejects numeric Level mismatches before fuzzy
    similarity is considered. It also evaluates the original URL path because
    archived page titles can be generic even when the slug is precise.
    """
    query_level = extract_level_identifier(query)
    candidate_level = extract_level_identifier(f"{candidate_title} {original_url}")
    if query_level is not None:
        if candidate_level is None:
            return 0.0
        if query_level != candidate_level:
            return 0.0

    original_path = unquote(urlparse(original_url).path).replace("/", " ") if original_url else ""
    score = max(
        title_similarity(query, candidate_title),
        title_similarity(query, original_path),
    )

    q_tokens = {
        token for token in normalize_title_key(query).split()
        if token not in _GENERIC_MATCH_TOKENS
    }
    candidate_tokens = set(normalize_title_key(f"{candidate_title} {original_path}").split())
    if q_tokens:
        coverage = len(q_tokens & candidate_tokens) / len(q_tokens)
        score = max(score, coverage)

    return round(score, 4)


def canonical_identity_keys(title: str, url: str = "") -> Set[str]:
    """Return exact normalized page identifiers from a title and URL path.

    Display titles on Wikidot can be generic (for example, ``Liminal Archives``)
    while the URL path is exact. Conversely, token overlap is not identity: a
    page called ``Baby Food Review`` is not the ``Baby Food`` article.
    """
    values = [title]
    parsed = urlparse(url)
    path = unquote(parsed.path or "").strip("/")
    if path:
        values.extend([path, path.rsplit("/", 1)[-1]])
    return {normalize_title_key(value) for value in values if normalize_title_key(value)}


def live_candidate_is_acceptable(query: str, candidate_title: str, candidate_url: str = "") -> bool:
    """Require exact canonical identity before a direct fetch substitutes a title.

    Search is intentionally allowed to find approximate ideas. Fetching a page on
    behalf of an exact user query is not: it must resolve to the same normalized
    title or URL slug. Level numbers are additionally identity-critical.
    """
    query_key = normalize_title_key(query)
    if not query_key:
        return False
    keys = canonical_identity_keys(candidate_title, candidate_url)
    if query_key in keys:
        return True

    query_level = extract_level_identifier(query)
    candidate_levels = {
        level
        for level in (
            extract_level_identifier(candidate_title),
            extract_level_identifier(unquote(urlparse(candidate_url).path)),
        )
        if level is not None
    }
    if query_level is not None:
        return candidate_levels == {query_level} and query_key in keys
    return False


def archive_candidate_is_acceptable(
    query: str,
    candidate_title: str,
    original_url: str = "",
    *,
    threshold: float = ARCHIVE_MIN_SIMILARITY,
) -> bool:
    # Archive title/path identity is stricter than ranking. Archive search may
    # use fuzzy scoring to order candidates, but only an exact canonical key may
    # be promoted to a page response.
    return (
        live_candidate_is_acceptable(query, candidate_title, original_url)
        and archive_candidate_match_score(query, candidate_title, original_url) >= threshold
    )


# =============================================================================
# 8. NORMALIZED RESULT TYPES
# =============================================================================

@dataclass
class SearchHit:
    source_id: str
    source_name: str
    canon: str
    title: str
    url: str
    snippet: str = ""
    score: float = 0.0
    page_id: Optional[int] = None
    archived: bool = False
    language: str = "en"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PagePayload:
    source_id: str
    source_name: str
    canon: str
    title: str
    url: str
    text: str
    headings: List[str]
    links: List[dict]
    image_urls: List[str]
    analysis: dict
    retrieved_at: str
    archived: bool = False
    archive_timestamp: Optional[str] = None
    language: str = "en"
    requested_query: Optional[str] = None
    resolved_via: str = "direct"
    truncated: bool = False
    upstream_diagnostics: List[dict] = field(default_factory=list)

    def to_dict(self, *, include_text: bool = True, action_safe: bool = False) -> dict:
        data = asdict(self)
        if not include_text:
            data.pop("text", None)
        if action_safe:
            # Keep the high-value page content, but cap auxiliary structures so
            # a single Action response cannot grow with page navigation chrome.
            data["headings"] = data.get("headings", [])[:40]
            data["links"] = data.get("links", [])[:100]
            data["image_urls"] = data.get("image_urls", [])[:30]
            data["upstream_diagnostics"] = data.get("upstream_diagnostics", [])[:8]
            analysis = dict(data.get("analysis") or {})
            sections = dict(analysis.get("section_extracts") or {})
            analysis["section_extracts"] = {
                key: compact_ws(str(value or ""))[:2_000]
                for key, value in sections.items()
            }
            signals = dict(analysis.get("named_signals") or {})
            analysis["named_signals"] = {
                key: list(value or [])[:20] for key, value in signals.items()
            }
            hazard = dict(analysis.get("heuristic_hazard_signal") or {})
            if "evidence" in hazard:
                hazard["evidence"] = list(hazard["evidence"] or [])[:8]
            analysis["heuristic_hazard_signal"] = hazard
            data["analysis"] = analysis
        data["provenance"] = {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "canon": self.canon,
            "url": self.url,
            "retrieved_at": self.retrieved_at,
            "archived": self.archived,
            "archive_timestamp": self.archive_timestamp,
        }
        return data


def action_page_payload(page: PagePayload) -> dict:
    """Serialize a page for a Custom GPT Action with a hard response budget.

    Upstream HTML is bounded before parsing, but parsed auxiliary fields can
    still expand independently (for example a page with many long link labels).
    Keep only the documented, high-value response shape and then apply a second
    compact fallback if needed.  This prevents an Action response from becoming
    unbounded through navigation chrome or unusual source markup.
    """
    data = page.to_dict(action_safe=True)
    for field_name, limit in {
        "source_id": 120,
        "source_name": 240,
        "canon": 240,
        "title": 500,
        "url": 2_048,
        "language": 32,
        "requested_query": 500,
        "resolved_via": 120,
        "retrieved_at": 80,
        "archive_timestamp": 80,
    }.items():
        data[field_name] = compact_ws(str(data.get(field_name) or ""))[:limit]
    data["text"] = str(data.get("text") or "")[:ACTION_ABSOLUTE_MAX_TEXT_CHARS]
    data["headings"] = [
        compact_ws(str(heading or ""))[:500]
        for heading in data.get("headings", [])[:40]
    ]
    data["links"] = [
        {
            "title": compact_ws(str(link.get("title") or ""))[:500],
            "url": str(link.get("url") or "")[:1_024],
        }
        for link in data.get("links", [])[:100]
        if isinstance(link, Mapping)
    ]
    data["image_urls"] = [str(url)[:1_024] for url in data.get("image_urls", [])[:30]]
    raw_analysis = dict(data.get("analysis") or {})
    raw_signals = dict(raw_analysis.get("named_signals") or {})
    named_signals = {}
    for key, values in list(raw_signals.items())[:12]:
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        named_signals[compact_ws(str(key or ""))[:80]] = [
            compact_ws(str(value or ""))[:300] for value in list(values)[:20]
        ]
    raw_hazard = dict(raw_analysis.get("heuristic_hazard_signal") or {})
    evidence = []
    for item in list(raw_hazard.get("evidence") or [])[:8]:
        if not isinstance(item, Mapping):
            continue
        evidence.append(
            {
                "term": compact_ws(str(item.get("term") or ""))[:120],
                "count": item.get("count") if isinstance(item.get("count"), (int, float)) else None,
                "weight": item.get("weight") if isinstance(item.get("weight"), (int, float)) else None,
            }
        )
    section_extracts = {}
    for key, value in list(dict(raw_analysis.get("section_extracts") or {}).items())[:8]:
        section_extracts[compact_ws(str(key or ""))[:80]] = compact_ws(str(value or ""))[:1_000]
    detected_sections = {
        compact_ws(str(key or ""))[:80]: bool(value)
        for key, value in list(dict(raw_analysis.get("detected_sections") or {}).items())[:12]
    }
    document_stats = {
        compact_ws(str(key or ""))[:80]: value
        for key, value in list(dict(raw_analysis.get("document_stats") or {}).items())[:12]
        if isinstance(value, (bool, int, float))
    }
    data["analysis"] = {
        "summary": compact_ws(str(raw_analysis.get("summary") or ""))[:3_000],
        "heuristic_hazard_signal": {
            "label": compact_ws(str(raw_hazard.get("label") or ""))[:120],
            "score": raw_hazard.get("score") if isinstance(raw_hazard.get("score"), (int, float)) else None,
            "method": compact_ws(str(raw_hazard.get("method") or ""))[:500],
            "evidence": evidence,
        },
        "named_signals": named_signals,
        "detected_sections": detected_sections,
        "section_extracts": section_extracts,
        "document_stats": document_stats,
    }
    data["upstream_diagnostics"] = [
        {
            "url": str(item.get("url") or "")[:1_024],
            "ok": bool(item.get("ok")),
            "status_code": item.get("status_code") if isinstance(item.get("status_code"), int) else None,
            "elapsed_ms": item.get("elapsed_ms") if isinstance(item.get("elapsed_ms"), int) else None,
            "error_type": compact_ws(str(item.get("error_type") or ""))[:120],
            "message": compact_ws(str(item.get("message") or ""))[:500],
        }
        for item in data.get("upstream_diagnostics", [])[:8]
        if isinstance(item, Mapping)
    ]
    data["provenance"] = {
        "source_id": data["source_id"],
        "source_name": data["source_name"],
        "canon": data["canon"],
        "url": data["url"],
        "retrieved_at": data["retrieved_at"],
        "archived": bool(data.get("archived")),
        "archive_timestamp": data["archive_timestamp"] or None,
    }
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 64_000:
        data["text"] = data["text"][:12_000]
        data["headings"] = data["headings"][:10]
        data["links"] = [
            {"title": link["title"][:160], "url": link["url"][:384]}
            for link in data["links"][:12]
        ]
        data["image_urls"] = [url[:384] for url in data["image_urls"][:8]]
        data["analysis"] = {
            "summary": data["analysis"]["summary"][:1_000],
            "heuristic_hazard_signal": data["analysis"]["heuristic_hazard_signal"],
            "named_signals": {
                key: values[:5]
                for key, values in list(data["analysis"]["named_signals"].items())[:6]
            },
            "detected_sections": data["analysis"]["detected_sections"],
            "section_extracts": {
                key: value[:300]
                for key, value in list(data["analysis"]["section_extracts"].items())[:4]
            },
            "document_stats": data["analysis"]["document_stats"],
        }
        data["upstream_diagnostics"] = data["upstream_diagnostics"][:2]
        data["action_payload_truncated"] = True
    return data


# =============================================================================
# 9. ADAPTER BASE CLASS
# =============================================================================

class BaseAdapter:
    def __init__(self, source: SourceConfig):
        self.source = source

    async def search(self, query: str, limit: int = 10) -> List[SearchHit]:
        raise NotImplementedError

    async def fetch_page(
        self,
        page: str,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> PagePayload:
        raise NotImplementedError

    async def fetch_hit(
        self,
        hit: SearchHit,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> PagePayload:
        """Fetch a previously returned search hit using its canonical locator."""
        return await self.fetch_page(
            hit.title,
            max_chars=max_chars,
            allow_archive_fallback=allow_archive_fallback,
        )

    async def recent(self, limit: int = 10) -> List[SearchHit]:
        raise BadSourceQuery(f"Source {self.source.id} does not support recent changes.")

    async def probe(self) -> dict:
        raise NotImplementedError


# =============================================================================
# 10. MEDIAWIKI / FANDOM ADAPTER
# =============================================================================

class MediaWikiAdapter(BaseAdapter):
    def __init__(self, source: SourceConfig):
        super().__init__(source)
        if not source.api_url:
            raise ValueError(f"MediaWiki source {source.id} requires api_url")

    async def _api(self, params: Mapping[str, Any]) -> dict:
        cache_key = f"mwapi:{self.source.id}:{stable_hash(json.dumps(dict(params), sort_keys=True, ensure_ascii=False))}"
        cached = await cache.get(cache_key)
        if cached is not None:
            return cached

        response, diagnostics = await network.get(
            self.source.api_url,
            params=params,
            json_preferred=True,
            retries=2,
            allow_404=False,
        )
        if response is None:
            raise SourceUnavailable(f"No response from {self.source.id}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceUnavailable(
                f"{self.source.id} returned non-JSON API data"
            ) from exc

        if isinstance(data, dict) and data.get("error"):
            code = data["error"].get("code", "mediawiki_api_error")
            info = data["error"].get("info", "Unknown MediaWiki API error")
            if code in {"missingtitle", "invalidtitle"}:
                raise PageNotFound(info)
            raise SourceUnavailable(f"MediaWiki API error {code}: {info}")

        await cache.set(cache_key, data)
        return data

    async def search(self, query: str, limit: int = 10) -> List[SearchHit]:
        query = normalize_query(query)
        limit = clamp_int(limit, 1, 50)
        if not query:
            raise BadSourceQuery("Search query cannot be empty.")

        data = await self._api(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "srprop": "snippet|titlesnippet|sectiontitle|wordcount|timestamp",
                "format": "json",
                "formatversion": 2,
                "utf8": 1,
            }
        )
        hits = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            if not title:
                continue
            encoded_title = quote(title.replace(" ", "_"), safe="_():+-")
            page_url = (
                self.source.page_url_template.format(title=encoded_title)
                if self.source.page_url_template
                else self.source.api_url
            )
            snippet_html = item.get("snippet", "")
            snippet = BeautifulSoup(snippet_html, "html.parser").get_text(" ", strip=True)
            similarity = title_similarity(query, title)
            hits.append(
                SearchHit(
                    source_id=self.source.id,
                    source_name=self.source.name,
                    canon=self.source.canon,
                    title=title,
                    url=page_url,
                    snippet=snippet,
                    score=round(similarity, 4),
                    page_id=item.get("pageid"),
                    archived=False,
                    language=self.source.language,
                )
            )

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits

    async def _fetch_exact_title(
        self,
        title: str,
        *,
        max_chars: int,
        requested_query: str,
        resolved_via: str,
    ) -> PagePayload:
        data = await self._api(
            {
                "action": "parse",
                "page": title,
                "prop": "text|displaytitle|sections|links|images|properties",
                "disabletoc": 1,
                "disableeditsection": 1,
                "format": "json",
                "formatversion": 2,
            }
        )
        parsed = data.get("parse")
        if not parsed:
            raise PageNotFound(f"Page '{title}' was not returned by {self.source.id}")

        html_text = parsed.get("text", "")
        resolved_title = parsed.get("title") or title
        encoded_title = quote(resolved_title.replace(" ", "_"), safe="_():+-")
        page_url = (
            self.source.page_url_template.format(title=encoded_title)
            if self.source.page_url_template
            else self.source.api_url
        )
        if not live_candidate_is_acceptable(requested_query, resolved_title, page_url):
            raise PageNotFound(
                f"MediaWiki resolved '{requested_query}' to a different page identity."
            )
        doc = sanitize_html_document(
            html_text,
            page_url=page_url,
            preferred_container_id="mw-content-text",
            max_chars=max_chars,
        )
        if doc.missing_page_signal or not doc.text:
            raise PageNotFound(f"MediaWiki page '{title}' has no readable content.")

        analysis = LoreAnalyzer.analyze(doc)
        return PagePayload(
            source_id=self.source.id,
            source_name=self.source.name,
            canon=self.source.canon,
            title=resolved_title or doc.title,
            url=page_url,
            text=doc.text,
            headings=doc.headings,
            links=doc.links,
            image_urls=doc.image_urls,
            analysis=analysis,
            retrieved_at=utc_now_iso(),
            archived=False,
            language=self.source.language,
            requested_query=requested_query,
            resolved_via=resolved_via,
            truncated=doc.character_count > len(doc.text),
        )

    async def fetch_page(
        self,
        page: str,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> PagePayload:
        page = normalize_query(page)
        if not page:
            raise BadSourceQuery("Page title cannot be empty.")
        max_chars = clamp_int(max_chars, 2000, ABSOLUTE_MAX_TEXT_CHARS)

        cache_key = f"page:{self.source.id}:{page.casefold()}:{max_chars}"
        cached = await cache.get(cache_key)
        if cached is not None:
            if cached.get("_negative") or cached.get("not_found"):
                raise PageNotFound(cached.get("message", f"No page matching '{page}' in {self.source.name}"))
            return PagePayload(**cached)

        # First try exact title.
        try:
            payload = await self._fetch_exact_title(
                page,
                max_chars=max_chars,
                requested_query=page,
                resolved_via="exact-title",
            )
            await cache.set(cache_key, asdict(payload))
            return payload
        except PageNotFound:
            pass

        # Then search and fetch the best title.
        hits = await self.search(page, limit=8)
        if not hits:
            message = f"No page matching '{page}' in {self.source.name}"
            await cache.set(cache_key, {"_negative": True, "message": message}, negative=True)
            raise PageNotFound(message)

        candidates = [
            hit
            for hit in hits
            if live_candidate_is_acceptable(page, hit.title, hit.url)
        ]
        if not candidates:
            message = f"No exact page identity matching '{page}' in {self.source.name}"
            await cache.set(cache_key, {"_negative": True, "message": message}, negative=True)
            raise PageNotFound(message)

        best = candidates[0]
        payload = await self._fetch_exact_title(
            best.title,
            max_chars=max_chars,
            requested_query=page,
            resolved_via="mediawiki-search",
        )
        await cache.set(cache_key, asdict(payload))
        return payload

    async def fetch_hit(
        self,
        hit: SearchHit,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> PagePayload:
        if hit.source_id != self.source.id:
            raise BadSourceQuery("Search hit belongs to a different source.")
        if not live_candidate_is_acceptable(hit.title, hit.title, hit.url):
            raise PageNotFound("Search result did not contain a safe canonical page identity.")
        return await self._fetch_exact_title(
            hit.title,
            max_chars=clamp_int(max_chars, 2000, ABSOLUTE_MAX_TEXT_CHARS),
            requested_query=hit.title,
            resolved_via="mediawiki-search-hit",
        )

    async def recent(self, limit: int = 10) -> List[SearchHit]:
        limit = clamp_int(limit, 1, 50)
        data = await self._api(
            {
                "action": "query",
                "list": "recentchanges",
                "rclimit": limit,
                "rcnamespace": 0,
                "rcprop": "title|ids|timestamp|comment|flags",
                "rctype": "edit|new",
                "format": "json",
                "formatversion": 2,
            }
        )
        hits = []
        for item in data.get("query", {}).get("recentchanges", []):
            title = item.get("title", "")
            if not title:
                continue
            encoded = quote(title.replace(" ", "_"), safe="_():+-")
            page_url = (
                self.source.page_url_template.format(title=encoded)
                if self.source.page_url_template
                else self.source.api_url
            )
            hits.append(
                SearchHit(
                    source_id=self.source.id,
                    source_name=self.source.name,
                    canon=self.source.canon,
                    title=title,
                    url=page_url,
                    snippet=compact_ws(item.get("comment", "")),
                    score=1.0,
                    page_id=item.get("pageid"),
                    archived=False,
                    language=self.source.language,
                )
            )
        return hits

    async def probe(self) -> dict:
        started = time.monotonic()
        try:
            data = await self._api(
                {
                    "action": "query",
                    "meta": "siteinfo",
                    "siprop": "general",
                    "format": "json",
                    "formatversion": 2,
                }
            )
            elapsed = int((time.monotonic() - started) * 1000)
            general = data.get("query", {}).get("general", {})
            return {
                "source_id": self.source.id,
                "ok": True,
                "latency_ms": elapsed,
                "sitename": general.get("sitename"),
                "generator": general.get("generator"),
                "server": general.get("server"),
            }
        except Exception as exc:
            return {
                "source_id": self.source.id,
                "ok": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }


# =============================================================================
# 11. WAYBACK ARCHIVE ADAPTER
# =============================================================================

class WaybackAdapter:
    AVAILABILITY_API = "https://archive.org/wayback/available"
    CDX_API = "https://web.archive.org/cdx/search/cdx"

    async def find_snapshot(self, url: str) -> Optional[dict]:
        response, _ = await network.get(
            self.AVAILABILITY_API,
            params={"url": url},
            json_preferred=True,
            retries=1,
            allow_404=False,
        )
        if response is None:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        closest = data.get("archived_snapshots", {}).get("closest")
        if not closest or not closest.get("available"):
            return None
        return closest

    async def search_domain(
        self,
        domain: str,
        query: str,
        *,
        limit: int = 12,
    ) -> List[dict]:
        limit = clamp_int(limit, 1, 50)
        cache_key = f"cdx:{domain}:{query.casefold()}:{limit}"
        cached = await cache.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "url": f"{domain}/*",
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            "limit": MAX_CDX_ROWS,
        }
        # httpx supports multiple filter keys if passed as sequence of tuples,
        # but dict list encoding is accepted by the CDX endpoint as repeated params.
        response, _ = await network.get(
            self.CDX_API,
            params=params,
            json_preferred=True,
            retries=1,
            allow_404=False,
        )
        if response is None:
            return []

        try:
            rows = response.json()
        except ValueError:
            return []
        if not rows or len(rows) < 2:
            return []

        headers = rows[0]
        query_key = normalize_title_key(query)
        scored: List[Tuple[float, dict]] = []
        for row in rows[1:]:
            item = dict(zip(headers, row))
            original = item.get("original", "")
            candidate_title = unquote(urlparse(original).path.strip("/").replace("-", " ")) or domain
            score = archive_candidate_match_score(query, candidate_title, original)
            if not archive_candidate_is_acceptable(query, candidate_title, original):
                continue
            timestamp = item.get("timestamp", "")
            archived_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
            scored.append(
                (
                    score,
                    {
                        "title": candidate_title,
                        "original_url": original,
                        "archive_url": archived_url,
                        "timestamp": timestamp,
                        "score": round(score, 4),
                    },
                )
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = [item for _, item in scored[:limit]]
        await cache.set(cache_key, results, ttl_seconds=3600)
        return results

    async def fetch_archived_url(
        self,
        archive_url: str,
        *,
        source: SourceConfig,
        requested_query: str,
        max_chars: int,
        resolved_via: str,
        archive_timestamp: Optional[str] = None,
    ) -> PagePayload:
        response, diagnostics = await network.get(
            archive_url,
            retries=1,
            allow_404=True,
        )
        if response is None or response.status_code == 404:
            raise PageNotFound(f"Archived snapshot not found for {requested_query}")

        doc = sanitize_html_document(
            response.text,
            page_url=str(response.url),
            preferred_container_id="page-content",
            max_chars=max_chars,
        )
        if not doc.text or doc.missing_page_signal:
            raise PageNotFound(f"Archived snapshot has no readable content for {requested_query}")

        if not archive_candidate_is_acceptable(
            requested_query,
            doc.title or requested_query,
            archive_url,
        ):
            raise PageNotFound(
                f"Archived snapshot identity did not safely match '{requested_query}'."
            )

        return PagePayload(
            source_id=source.id,
            source_name=source.name,
            canon=source.canon,
            title=doc.title or requested_query,
            url=str(response.url),
            text=doc.text,
            headings=doc.headings,
            links=doc.links,
            image_urls=doc.image_urls,
            analysis=LoreAnalyzer.analyze(doc),
            retrieved_at=utc_now_iso(),
            archived=True,
            archive_timestamp=archive_timestamp,
            language=source.language,
            requested_query=requested_query,
            resolved_via=resolved_via,
            truncated=doc.character_count > len(doc.text),
            upstream_diagnostics=[asdict(d) for d in diagnostics],
        )


wayback = WaybackAdapter()


# =============================================================================
# 12. WIKIDOT ADAPTER
# =============================================================================

class WikidotAdapter(BaseAdapter):
    SEARCH_PATH_TEMPLATE = "/search:site/q/{query}"
    RECENT_PATH = "/system:recent-changes"

    async def _try_url_candidates(
        self,
        urls: Sequence[str],
    ) -> Tuple[Optional[httpx.Response], Optional[str], List[dict], bool]:
        """
        Return:
          response, successful_url, diagnostics, any_source_reachable

        This distinction prevents network failures from being mislabeled as 404.
        """
        all_diagnostics: List[dict] = []
        reachable = False

        # Preserve slug/base priority. Racing every candidate allowed the fastest
        # page to win, even when it was not the intended canonical slug, and
        # amplified one public request into dozens of outbound requests.
        for url in urls[:MAX_WIKIDOT_URL_CANDIDATES]:
            try:
                response, diagnostics = await network.get(
                    url,
                    retries=1,
                    allow_404=True,
                )
                all_diagnostics.extend(asdict(d) for d in diagnostics)
            except Exception as exc:
                all_diagnostics.append(
                    {
                        "url": url,
                        "ok": False,
                        "status_code": None,
                        "elapsed_ms": 0,
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
                )
                continue
            if response is None:
                continue
            if response.status_code == 404:
                reachable = True
                continue
            reachable = True
            if 200 <= response.status_code < 300:
                doc = sanitize_html_document(
                    response.text,
                    page_url=str(response.url),
                    preferred_container_id="page-content",
                    max_chars=5000,
                )
                if doc.missing_page_signal:
                    continue
                return response, str(response.url), all_diagnostics, reachable

        return None, None, all_diagnostics, reachable

    def _page_urls(self, page: str) -> List[str]:
        slugs = generate_slug_candidates(page, language=self.source.language)
        urls: List[str] = []
        for slug in slugs:
            for base in self.source.base_urls:
                url = f"{base.rstrip('/')}/{quote(slug, safe=':_-./')}"
                if url not in urls:
                    urls.append(url)
        return urls[:MAX_WIKIDOT_URL_CANDIDATES]

    async def _live_fetch(
        self,
        page: str,
        *,
        max_chars: int,
    ) -> PagePayload:
        urls = self._page_urls(page)
        if not urls:
            raise BadSourceQuery("Page query cannot be empty.")

        response, successful_url, diagnostics, reachable = await self._try_url_candidates(urls)
        if response is None or successful_url is None:
            if reachable:
                raise PageNotFound(
                    f"Page '{page}' not found on reachable live host for {self.source.name}"
                )
            raise SourceUnavailable(
                f"No live host for {self.source.name} could be reached."
            )

        doc = sanitize_html_document(
            response.text,
            page_url=successful_url,
            preferred_container_id="page-content",
            max_chars=max_chars,
        )
        if not doc.text or doc.missing_page_signal:
            raise PageNotFound(f"Page '{page}' resolved to a soft 404.")
        if not live_candidate_is_acceptable(page, doc.title or page, successful_url):
            raise PageNotFound(
                f"Live Wikidot result did not safely match requested page '{page}'."
            )

        return PagePayload(
            source_id=self.source.id,
            source_name=self.source.name,
            canon=self.source.canon,
            title=doc.title or page,
            url=successful_url,
            text=doc.text,
            headings=doc.headings,
            links=doc.links,
            image_urls=doc.image_urls,
            analysis=LoreAnalyzer.analyze(doc),
            retrieved_at=utc_now_iso(),
            archived=False,
            language=self.source.language,
            requested_query=page,
            resolved_via="wikidot-slug-matrix",
            truncated=doc.character_count > len(doc.text),
            upstream_diagnostics=diagnostics[-20:],
        )

    async def _archive_search(self, query: str, limit: int) -> List[SearchHit]:
        results: List[SearchHit] = []
        for domain in self.source.archive_domains:
            try:
                rows = await wayback.search_domain(domain, query, limit=limit)
            except Exception as exc:
                logger.warning("Wayback search failed for %s: %s", domain, exc)
                continue
            for item in rows:
                results.append(
                    SearchHit(
                        source_id=self.source.id,
                        source_name=self.source.name,
                        canon=self.source.canon,
                        title=item["title"],
                        url=item["archive_url"],
                        snippet=f"Archived snapshot {item.get('timestamp', '')}",
                        score=item.get("score", 0.0),
                        archived=True,
                        language=self.source.language,
                    )
                )
        results.sort(key=lambda hit: hit.score, reverse=True)
        return results[:limit]

    async def search(self, query: str, limit: int = 10) -> List[SearchHit]:
        query = normalize_query(query)
        limit = clamp_int(limit, 1, 50)
        if not query:
            raise BadSourceQuery("Search query cannot be empty.")

        cache_key = f"wdsearch:{self.source.id}:{query.casefold()}:{limit}"
        cached = await cache.get(cache_key)
        if cached is not None:
            return [SearchHit(**item) for item in cached]

        live_hits: List[SearchHit] = []
        any_reachable = False
        diagnostics: List[dict] = []

        search_urls = [
            f"{base.rstrip('/')}{self.SEARCH_PATH_TEMPLATE.format(query=quote_plus(query))}"
            for base in self.source.base_urls
        ]

        response, successful_url, diagnostics, reachable = await self._try_url_candidates(search_urls)
        any_reachable = reachable

        if response is not None and successful_url:
            soup = BeautifulSoup(response.text, "html.parser")
            container = _best_content_container(soup, "page-content")
            seen: Set[str] = set()
            for anchor in container.find_all("a", href=True):
                href = urljoin(successful_url, anchor.get("href"))
                parsed = urlparse(href)
                if not parsed.path or parsed.path.startswith(("/search:", "/system:")):
                    continue
                title = compact_ws(anchor.get_text(" ", strip=True))
                if not title or len(title) > 160:
                    continue
                normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if normalized_url in seen:
                    continue
                seen.add(normalized_url)
                relevance = round(title_similarity(query, title), 4)
                if relevance < LIVE_SEARCH_MIN_SIMILARITY:
                    continue
                live_hits.append(
                    SearchHit(
                        source_id=self.source.id,
                        source_name=self.source.name,
                        canon=self.source.canon,
                        title=title,
                        url=normalized_url,
                        snippet="",
                        score=relevance,
                        archived=False,
                        language=self.source.language,
                    )
                )
                if len(live_hits) >= limit * 4:
                    break

        live_hits.sort(key=lambda hit: hit.score, reverse=True)
        live_hits = live_hits[:limit]

        # If site search is missing/empty, try direct resolution as a search hit.
        if not live_hits:
            try:
                page = await self._live_fetch(query, max_chars=5000)
                live_hits = [
                    SearchHit(
                        source_id=self.source.id,
                        source_name=self.source.name,
                        canon=self.source.canon,
                        title=page.title,
                        url=page.url,
                        snippet=page.analysis.get("summary", "")[:600],
                        score=1.0,
                        archived=False,
                        language=self.source.language,
                    )
                ]
            except PageNotFound:
                pass
            except SourceUnavailable:
                any_reachable = False

        # Explicit archive fallback only for sources configured with archive domains.
        if not live_hits and self.source.archive_domains:
            archive_hits = await self._archive_search(query, limit)
            if archive_hits:
                await cache.set(cache_key, [hit.to_dict() for hit in archive_hits], ttl_seconds=3600)
                return archive_hits

        if not live_hits and not any_reachable:
            raise SourceUnavailable(
                f"{self.source.name} is unreachable and no archive search result was found for '{query}'."
            )

        await cache.set(cache_key, [hit.to_dict() for hit in live_hits])
        return live_hits

    async def _archive_fetch(
        self,
        page: str,
        *,
        max_chars: int,
    ) -> PagePayload:
        # 1) Search CDX domain index for the requested concept.
        archive_hits = await self._archive_search(page, limit=8)
        if archive_hits:
            best = archive_hits[0]
            timestamp_match = re.search(r"/web/(\d+)", best.url)
            timestamp = timestamp_match.group(1) if timestamp_match else None
            return await wayback.fetch_archived_url(
                best.url,
                source=self.source,
                requested_query=page,
                max_chars=max_chars,
                resolved_via="wayback-cdx-search",
                archive_timestamp=timestamp,
            )

        # 2) Check exact candidate URLs via availability API.
        for url in self._page_urls(page)[:MAX_ARCHIVE_AVAILABILITY_CHECKS]:
            try:
                snapshot = await wayback.find_snapshot(url)
            except Exception:
                continue
            if snapshot:
                return await wayback.fetch_archived_url(
                    snapshot["url"],
                    source=self.source,
                    requested_query=page,
                    max_chars=max_chars,
                    resolved_via="wayback-availability",
                    archive_timestamp=snapshot.get("timestamp"),
                )

        raise PageNotFound(
            f"No live or archived page matching '{page}' for {self.source.name}"
        )

    async def fetch_page(
        self,
        page: str,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> PagePayload:
        page = normalize_query(page)
        max_chars = clamp_int(max_chars, 2000, ABSOLUTE_MAX_TEXT_CHARS)
        cache_key = f"page:{self.source.id}:{page.casefold()}:{max_chars}:{allow_archive_fallback}"
        cached = await cache.get(cache_key)
        if cached is not None:
            if cached.get("_negative"):
                raise PageNotFound(cached.get("message", "Page not found"))
            return PagePayload(**cached)

        live_error: Optional[Exception] = None
        try:
            payload = await self._live_fetch(page, max_chars=max_chars)
            await cache.set(cache_key, asdict(payload))
            return payload
        except (PageNotFound, SourceUnavailable) as exc:
            live_error = exc

        # Search live source before archive fallback; useful when the title differs
        # from the user's phrase.
        if not isinstance(live_error, SourceUnavailable):
            try:
                hits = await self.search(page, limit=8)
                live_hits = [hit for hit in hits if not hit.archived]
                candidates = [
                    hit
                    for hit in live_hits
                    if live_candidate_is_acceptable(page, hit.title, hit.url)
                ]
                if candidates:
                    best = candidates[0]
                    path = unquote(urlparse(best.url).path.strip("/"))
                    payload = await self._live_fetch(path or best.title, max_chars=max_chars)
                    payload.requested_query = page
                    payload.resolved_via = "wikidot-site-search"
                    await cache.set(cache_key, asdict(payload))
                    return payload
            except (PageNotFound, SourceUnavailable):
                pass

        if allow_archive_fallback and self.source.archive_domains:
            try:
                payload = await self._archive_fetch(page, max_chars=max_chars)
                await cache.set(cache_key, asdict(payload), ttl_seconds=3600)
                return payload
            except PageNotFound:
                pass

        message = str(live_error) if live_error else f"Page '{page}' not found."
        await cache.set(
            cache_key,
            {"_negative": True, "message": message},
            negative=True,
        )
        if isinstance(live_error, SourceUnavailable):
            raise live_error
        raise PageNotFound(message)

    async def fetch_hit(
        self,
        hit: SearchHit,
        *,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> PagePayload:
        if hit.source_id != self.source.id:
            raise BadSourceQuery("Search hit belongs to a different source.")
        bounded_chars = clamp_int(max_chars, 2000, ABSOLUTE_MAX_TEXT_CHARS)
        if hit.archived:
            if not allow_archive_fallback:
                raise PageNotFound(
                    "A matching historical archive candidate exists, but archive fallback is disabled."
                )
            timestamp_match = re.search(r"/web/(\d+)", hit.url)
            payload = await wayback.fetch_archived_url(
                hit.url,
                source=self.source,
                requested_query=hit.title,
                max_chars=bounded_chars,
                resolved_via="wayback-search-hit",
                archive_timestamp=timestamp_match.group(1) if timestamp_match else None,
            )
        else:
            locator = unquote(urlparse(hit.url).path.strip("/")) or hit.title
            payload = await self._live_fetch(locator, max_chars=bounded_chars)
            payload.requested_query = hit.title
            payload.resolved_via = "wikidot-search-hit"
        if not live_candidate_is_acceptable(hit.title, payload.title, payload.url):
            raise PageNotFound("Search hit did not resolve to its canonical page identity.")
        return payload

    async def recent(self, limit: int = 10) -> List[SearchHit]:
        limit = clamp_int(limit, 1, 50)
        urls = [f"{base.rstrip('/')}{self.RECENT_PATH}" for base in self.source.base_urls]
        response, successful_url, diagnostics, reachable = await self._try_url_candidates(urls)
        if response is None or not successful_url:
            if reachable:
                return []
            raise SourceUnavailable(f"{self.source.name} recent changes page is unreachable.")

        soup = BeautifulSoup(response.text, "html.parser")
        container = _best_content_container(soup, "page-content")
        hits: List[SearchHit] = []
        seen = set()
        for anchor in container.find_all("a", href=True):
            title = compact_ws(anchor.get_text(" ", strip=True))
            href = urljoin(successful_url, anchor.get("href"))
            parsed = urlparse(href)
            if not title or not parsed.path or parsed.path.startswith(("/system:", "/search:")):
                continue
            if href in seen:
                continue
            seen.add(href)
            hits.append(
                SearchHit(
                    source_id=self.source.id,
                    source_name=self.source.name,
                    canon=self.source.canon,
                    title=title,
                    url=href,
                    score=1.0,
                    archived=False,
                    language=self.source.language,
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def probe(self) -> dict:
        started = time.monotonic()
        diagnostics = []
        any_reachable = False
        for base in self.source.base_urls:
            try:
                response, attempt_diags = await network.get(
                    f"{base.rstrip('/')}/",
                    retries=0,
                    allow_404=True,
                )
                diagnostics.extend(asdict(d) for d in attempt_diags)
                if response is not None and response.status_code < 500:
                    any_reachable = True
                    return {
                        "source_id": self.source.id,
                        "ok": True,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "resolved_base_url": base,
                        "status_code": response.status_code,
                    }
            except Exception as exc:
                diagnostics.append({"url": base, "error": f"{type(exc).__name__}: {exc}"})

        return {
            "source_id": self.source.id,
            "ok": any_reachable,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "diagnostics": diagnostics[-10:],
        }


# =============================================================================
# 13. ADAPTER FACTORY AND DYNAMIC SOURCES
# =============================================================================

def adapter_for(source: SourceConfig) -> BaseAdapter:
    if source.kind == SourceKind.MEDIAWIKI:
        return MediaWikiAdapter(source)
    if source.kind == SourceKind.WIKIDOT:
        return WikidotAdapter(source)
    raise BadSourceQuery(f"No adapter implemented for source kind '{source.kind.value}'.")


def dynamic_fandom_source(site: str) -> SourceConfig:
    host = validate_dynamic_site_hostname(site, ".fandom.com")
    site_id = host.split(".fandom.com", 1)[0].replace(".", "-")
    return SourceConfig(
        id=f"dynamic-fandom:{site_id}",
        name=f"Dynamic Fandom: {host}",
        kind=SourceKind.MEDIAWIKI,
        canon=f"Dynamic Fandom ({host})",
        priority=90,
        api_url=f"https://{host}/api.php",
        page_url_template=f"https://{host}/wiki/{{title}}",
        aliases=(),
        tags=("dynamic", "fandom"),
        notes="Dynamically constructed user-requested Fandom source.",
    )


def dynamic_wikidot_source(site: str) -> SourceConfig:
    host = validate_dynamic_site_hostname(site, ".wikidot.com")
    site_id = host.split(".wikidot.com", 1)[0].replace(".", "-")
    return SourceConfig(
        id=f"dynamic-wikidot:{site_id}",
        name=f"Dynamic Wikidot: {host}",
        kind=SourceKind.WIKIDOT,
        canon=f"Dynamic Wikidot ({host})",
        priority=90,
        base_urls=(f"https://{host}",),
        aliases=(),
        tags=("dynamic", "wikidot"),
        archive_domains=(host,),
        notes="Dynamically constructed user-requested Wikidot source.",
    )


# =============================================================================
# 14. OMNI SEARCH, RESOLUTION, COMPARISON, GRAPH
# =============================================================================

class OmniLoreEngine:
    def __init__(self, registry: SourceRegistry):
        self.registry = registry

    def _select_sources(
        self,
        *,
        scope: str = "core",
        source_ids: Optional[Sequence[str]] = None,
        languages: Optional[Sequence[str]] = None,
    ) -> List[SourceConfig]:
        if source_ids:
            deduped_ids: List[str] = []
            for source_id in source_ids:
                normalized = (source_id or "").strip()
                if normalized and normalized not in deduped_ids:
                    deduped_ids.append(normalized)
            if len(deduped_ids) > MAX_SOURCE_IDS_PER_REQUEST:
                raise BadSourceQuery(
                    f"At most {MAX_SOURCE_IDS_PER_REQUEST} source IDs may be requested at once."
                )
            selected = [self.registry.get(source_id) for source_id in deduped_ids]
        else:
            scope_key = scope.casefold()
            if scope_key == "core":
                selected = [
                    self.registry.get("fandom-main"),
                    self.registry.get("wikidot-main"),
                    self.registry.get("liminal-archives"),
                    self.registry.get("freewriting-fandom"),
                    self.registry.get("kanepixels-fandom"),
                ]
            elif scope_key == "international":
                selected = self.registry.list(tag="international")
            elif scope_key in {"extended", "all"}:
                selected = self.registry.list()
            else:
                raise BadSourceQuery(
                    "scope must be one of: core, international, extended, all"
                )

        if languages:
            lang_set = {lang.casefold() for lang in languages}
            selected = [s for s in selected if s.language.casefold() in lang_set]

        return sorted(selected, key=lambda source: source.priority)

    async def search(
        self,
        query: str,
        *,
        scope: str = "core",
        source_ids: Optional[Sequence[str]] = None,
        languages: Optional[Sequence[str]] = None,
        per_source_limit: int = 6,
        total_limit: int = 40,
    ) -> dict:
        query = normalize_query(query)
        if not query:
            raise BadSourceQuery("Omni search query cannot be empty.")
        if len(query) > MAX_QUERY_CHARS:
            raise BadSourceQuery(f"Omni search query must not exceed {MAX_QUERY_CHARS} characters.")

        sources = self._select_sources(
            scope=scope,
            source_ids=source_ids,
            languages=languages,
        )
        per_source_limit = clamp_int(per_source_limit, 1, 15)
        total_limit = clamp_int(total_limit, 1, 100)

        semaphore = asyncio.Semaphore(MAX_OMNI_CONCURRENCY)

        async def search_one(source: SourceConfig):
            async with semaphore:
                started = time.monotonic()
                try:
                    hits = await adapter_for(source).search(query, limit=per_source_limit)
                    return {
                        "source": source,
                        "hits": hits,
                        "error": None,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }
                except Exception as exc:
                    return {
                        "source": source,
                        "hits": [],
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }

        rows = await asyncio.gather(*(search_one(source) for source in sources))

        all_hits: List[SearchHit] = []
        source_status = []
        for row in rows:
            source = row["source"]
            hits = row["hits"]
            # Add priority bonus while preserving title relevance.
            for hit in hits:
                priority_bonus = max(0.0, (100 - source.priority) / 500.0)
                hit.score = round(min(1.2, hit.score + priority_bonus), 4)
                all_hits.append(hit)
            source_status.append(
                {
                    "source_id": source.id,
                    "source_name": source.name,
                    "ok": row["error"] is None,
                    "result_count": len(hits),
                    "elapsed_ms": row["elapsed_ms"],
                    "error": row["error"],
                }
            )

        # Deduplicate only within the same source, never merge canon-distinct pages.
        deduped: Dict[Tuple[str, str], SearchHit] = {}
        for hit in all_hits:
            key = (hit.source_id, normalize_title_key(hit.title))
            existing = deduped.get(key)
            if existing is None or hit.score > existing.score:
                deduped[key] = hit

        ranked = sorted(deduped.values(), key=lambda hit: hit.score, reverse=True)
        ranked = ranked[:total_limit]
        successful_sources = sum(1 for row in source_status if row["ok"])
        if sources and successful_sources == 0:
            raise SourceUnavailable(
                f"No selected source could be searched reliably for '{query}'."
            )

        return {
            "ok": True,
            "status": "ok" if ranked else "search_no_match",
            "query": query,
            "scope": scope,
            "results": [hit.to_dict() for hit in ranked],
            "source_status": source_status,
            "meta": {
                "searched_sources": len(sources),
                "successful_sources": successful_sources,
                "failed_sources": sum(1 for row in source_status if not row["ok"]),
                "result_count": len(ranked),
                "timestamp": utc_now_iso(),
            },
        }

    async def resolve_and_fetch(
        self,
        query: str,
        *,
        scope: str = "core",
        source_ids: Optional[Sequence[str]] = None,
        max_chars: int = DEFAULT_MAX_TEXT_CHARS,
        allow_archive_fallback: bool = True,
    ) -> dict:
        search_result = await self.search(
            query,
            scope=scope,
            source_ids=source_ids,
            per_source_limit=5,
            total_limit=25,
        )
        hits = search_result["results"]
        if not hits:
            if search_result.get("meta", {}).get("successful_sources", 0) == 0:
                raise SourceUnavailable(
                    f"No selected source could be searched for '{query}'."
                )
            raise PageNotFound(f"No lore page matching '{query}' across selected sources.")

        eligible_hits = [
            hit for hit in hits
            if allow_archive_fallback or not bool(hit.get("archived"))
        ]
        if not eligible_hits:
            raise PageNotFound(
                f"Only historical archive candidates matched '{query}', and archive fallback is disabled."
            )

        attempts = []
        saw_not_found = False
        saw_unavailable = False
        for hit in eligible_hits[:12]:
            source = self.registry.get(hit["source_id"])
            try:
                page = await adapter_for(source).fetch_hit(
                    SearchHit(**hit),
                    max_chars=max_chars,
                    allow_archive_fallback=allow_archive_fallback,
                )
                return {
                    "ok": True,
                    "query": query,
                    "selected_result": hit,
                    "page": page.to_dict(),
                    "alternatives": eligible_hits[1:8],
                    "attempts": attempts,
                }
            except PageNotFound as exc:
                saw_not_found = True
                attempts.append(
                    {
                        "source_id": source.id,
                        "title": hit["title"],
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )
            except SourceUnavailable as exc:
                saw_unavailable = True
                attempts.append(
                    {
                        "source_id": source.id,
                        "title": hit["title"],
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )
            except Exception as exc:
                attempts.append(
                    {
                        "source_id": source.id,
                        "title": hit["title"],
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )

        if saw_not_found:
            raise PageNotFound(
                f"No safely resolvable lore page matching '{query}' across selected sources."
            )
        if saw_unavailable:
            raise SourceUnavailable(
                f"Search found candidates for '{query}', but upstream retrieval was unavailable."
            )
        raise SourceUnavailable(
            f"Search found candidates for '{query}', but all top candidate fetches failed."
        )

    async def compare(
        self,
        query: str,
        *,
        source_ids: Optional[Sequence[str]] = None,
        scope: str = "core",
        max_chars_per_source: int = 16000,
        allow_archive_fallback: bool = False,
    ) -> dict:
        """Compare a concept across sources using compact canon-separated records.

        Full page text is fetched internally for analysis but is deliberately not
        returned by this endpoint. That keeps Custom GPT Action payloads bounded
        and prevents multi-source comparisons from exceeding tool response limits.
        Full text remains available through /source/page when deeper inspection is
        needed.
        """
        max_chars_per_source = clamp_int(max_chars_per_source, 4000, 40000)
        sources = self._select_sources(scope=scope, source_ids=source_ids)[:MAX_COMPARE_SOURCES]
        semaphore = asyncio.Semaphore(MAX_OMNI_CONCURRENCY)

        def compact_sections(section_extracts: Mapping[str, Any]) -> dict:
            compact: Dict[str, str] = {}
            for name, value in section_extracts.items():
                text = compact_ws(str(value or ""))
                if text:
                    compact[name] = text[:COMPARE_SECTION_EXCERPT_CHARS]
            return compact

        async def fetch_one(source: SourceConfig):
            async with semaphore:
                try:
                    page = await adapter_for(source).fetch_page(
                        query,
                        max_chars=max_chars_per_source,
                        allow_archive_fallback=allow_archive_fallback,
                    )
                    payload = page.to_dict()
                    analysis = payload.get("analysis", {})
                    return {
                        "source_id": source.id,
                        "source_name": source.name,
                        "canon": source.canon,
                        "ok": True,
                        "title": payload.get("title"),
                        "url": payload.get("url"),
                        "archived": payload.get("archived", False),
                        "archive_timestamp": payload.get("archive_timestamp"),
                        "language": payload.get("language"),
                        "summary": compact_ws(analysis.get("summary", ""))[:1800],
                        "detected_sections": analysis.get("detected_sections", {}),
                        "named_signals": analysis.get("named_signals", {}),
                        "heuristic_hazard_signal": analysis.get("heuristic_hazard_signal", {}),
                        "section_extracts": compact_sections(analysis.get("section_extracts", {})),
                        "resolved_via": payload.get("resolved_via"),
                    }
                except Exception as exc:
                    return {
                        "source_id": source.id,
                        "source_name": source.name,
                        "canon": source.canon,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }

        records = await asyncio.gather(*(fetch_one(source) for source in sources))
        successful = [record for record in records if record["ok"]]
        if sources and not successful:
            failure_types = {record.get("error_type") for record in records}
            if failure_types and failure_types <= {"PageNotFound"}:
                raise PageNotFound(
                    f"No selected source contained a page matching '{query}'."
                )
            raise SourceUnavailable(
                f"No selected source could be fetched reliably for comparison '{query}'."
            )

        comparison_matrix = [
            {
                "source_id": record["source_id"],
                "canon": record["canon"],
                "title": record.get("title"),
                "summary": record.get("summary", ""),
                "detected_sections": record.get("detected_sections", {}),
                "named_signals": record.get("named_signals", {}),
                "heuristic_hazard_signal": record.get("heuristic_hazard_signal", {}),
                "archived": record.get("archived", False),
                "url": record.get("url"),
            }
            for record in successful
        ]

        return {
            "ok": bool(successful),
            "query": query,
            "records": records,
            "comparison_matrix": comparison_matrix,
            "meta": {
                "requested_sources": len(sources),
                "successful_sources": len(successful),
                "failed_sources": len(records) - len(successful),
                "timestamp": utc_now_iso(),
                "payload_mode": "compact-comparison",
                "note": (
                    "Canons are preserved separately. Full page text is intentionally omitted "
                    "from comparison responses; fetch individual pages for deeper reading."
                ),
            },
        }

    async def recent_across_sources(
        self,
        *,
        scope: str = "core",
        source_ids: Optional[Sequence[str]] = None,
        per_source_limit: int = 8,
        total_limit: int = 40,
    ) -> dict:
        sources = self._select_sources(scope=scope, source_ids=source_ids)
        semaphore = asyncio.Semaphore(MAX_OMNI_CONCURRENCY)

        async def recent_one(source: SourceConfig):
            async with semaphore:
                try:
                    hits = await adapter_for(source).recent(limit=per_source_limit)
                    return {"source": source, "hits": hits, "error": None}
                except Exception as exc:
                    return {"source": source, "hits": [], "error": f"{type(exc).__name__}: {exc}"}

        rows = await asyncio.gather(*(recent_one(source) for source in sources))
        hits = []
        status = []
        for row in rows:
            hits.extend(row["hits"])
            status.append(
                {
                    "source_id": row["source"].id,
                    "ok": row["error"] is None,
                    "count": len(row["hits"]),
                    "error": row["error"],
                }
            )
        if sources and not any(item["ok"] for item in status):
            raise SourceUnavailable("No selected source could provide recent changes reliably.")
        return {
            "ok": True,
            "results": [hit.to_dict() for hit in hits[:total_limit]],
            "source_status": status,
            "meta": {"timestamp": utc_now_iso()},
        }

    async def build_link_graph(
        self,
        source_id: str,
        page: str,
        *,
        depth: int = 1,
        max_nodes: int = 25,
    ) -> dict:
        depth = clamp_int(depth, 1, 2)
        max_nodes = clamp_int(max_nodes, 2, 60)
        source = self.registry.get(source_id)
        adapter = adapter_for(source)

        root = await adapter.fetch_page(
            page,
            max_chars=40000,
            allow_archive_fallback=False,
        )
        nodes: Dict[str, dict] = {
            root.url: {
                "id": stable_hash(root.url),
                "title": root.title,
                "url": root.url,
                "depth": 0,
                "source_id": root.source_id,
                "canon": root.canon,
                "archived": root.archived,
                "retrieved_at": root.retrieved_at,
            }
        }
        edges: List[dict] = []

        frontier = [(root, 0)]
        visited_pages = {root.url}

        while frontier and len(nodes) < max_nodes:
            current, current_depth = frontier.pop(0)
            if current_depth >= depth:
                continue

            candidate_links = current.links[: max_nodes * 2]
            for link in candidate_links:
                if len(nodes) >= max_nodes:
                    break
                url = link.get("url", "")
                if not self._link_belongs_to_source(url, source):
                    continue
                title = link.get("title") or unquote(urlparse(url).path.rsplit("/", 1)[-1])
                if not title:
                    continue

                node_id = stable_hash(url)
                nodes.setdefault(
                    url,
                    {
                        "id": node_id,
                        "title": title,
                        "url": url,
                        "depth": current_depth + 1,
                        "source_id": current.source_id,
                        "canon": current.canon,
                        "archived": False,
                    },
                )
                edges.append(
                    {
                        "from": stable_hash(current.url),
                        "to": node_id,
                        "label": "links_to",
                        "source_id": current.source_id,
                        "archived": current.archived,
                    }
                )

                if current_depth + 1 < depth and url not in visited_pages:
                    visited_pages.add(url)
                    try:
                        child = await adapter.fetch_page(
                            title,
                            max_chars=20000,
                            allow_archive_fallback=False,
                        )
                        frontier.append((child, current_depth + 1))
                    except Exception:
                        continue

        return {
            "ok": True,
            "source_id": source.id,
            "root": stable_hash(root.url),
            "nodes": list(nodes.values()),
            "edges": edges,
            "meta": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "max_depth": depth,
            },
        }

    @staticmethod
    def _link_belongs_to_source(url: str, source: SourceConfig) -> bool:
        host = (urlparse(url).hostname or "").casefold()
        allowed_hosts = set()
        if source.api_url:
            allowed_hosts.add((urlparse(source.api_url).hostname or "").casefold())
        if source.page_url_template:
            allowed_hosts.add((urlparse(source.page_url_template).hostname or "").casefold())
        for base in source.base_urls:
            allowed_hosts.add((urlparse(base).hostname or "").casefold())
        return host in allowed_hosts


omni = OmniLoreEngine(registry)


# =============================================================================
# 15. FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title=APP_NAME,
    description=(
        "Live multi-source Backrooms lore retrieval gateway for Custom GPT Actions. "
        "Supports canon-separated search, page resolution, source comparison, "
        "recent changes, Fandom/MediaWiki, Wikidot, Liminal Archives live-first "
        "resolution with explicit archive fallback, and dynamic supported-wiki reads."
    ),
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chatgpt.com", "https://chat.openai.com"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# The API remains usable without an account, so public retrieval needs a small
# process-local brake against outbound fan-out.  This is deliberately a
# best-effort guard (a CDN/WAF can add stronger shared limits in production);
# individual routes still clamp result sizes and concurrency.
public_outbound_limiter = FixedWindowRateLimiter(max_keys=8_192)
PUBLIC_OUTBOUND_PATH_PREFIXES = (
    "/sources/health",
    "/source/",
    "/omni/",
    "/archives/",
    "/dynamic/",
    "/url/read",
    "/fandom/",
    "/wikidot/",
    "/cinematic/",
    "/selftest",
    "/atlas/index/search",
    "/atlas/media/commons/search",
)


@app.middleware("http")
async def public_outbound_rate_limit(request: Request, call_next):
    path = request.url.path
    if request.method == "GET" and any(path.startswith(prefix) for prefix in PUBLIC_OUTBOUND_PATH_PREFIXES):
        client = request.client.host if request.client else "unknown"
        if not public_outbound_limiter.allow(
            "global",
            limit=PUBLIC_OUTBOUND_GLOBAL_PER_MINUTE,
            window_seconds=60,
        ) or not public_outbound_limiter.allow(
            f"client:{client}",
            limit=PUBLIC_OUTBOUND_REQUESTS_PER_MINUTE,
            window_seconds=60,
        ):
            return error_response(
                429,
                "rate_limited",
                "Public retrieval rate limit exceeded. Try again shortly.",
                retryable=True,
                headers={"Retry-After": "60"},
            )
    return await call_next(request)


@app.middleware("http")
async def request_telemetry(request: Request, call_next):
    request_id = stable_hash(f"{time.time_ns()}:{request.client.host if request.client else 'unknown'}", 12)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled request failure | id=%s | path=%s", request_id, request.url.path)
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-BackroomsGPT-Version"] = APP_VERSION
    response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    logger.info(
        "%s %s -> %s in %sms | id=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        request_id,
    )
    return response


@app.on_event("shutdown")
async def shutdown_network_client():
    await network.close()


# =============================================================================
# 16. ROOT, HEALTH, METADATA
# =============================================================================

@app.get("/", tags=["System"])
async def root():
    return {
        "ok": True,
        "service": APP_NAME,
        "version": APP_VERSION,
        "build": BUILD_NAME,
        "status": "live",
        "uptime_seconds": round(time.monotonic() - SERVICE_STARTED_AT, 2),
        "source_count": len(registry.all_ids()),
        "docs": "/docs",
        "openapi": "/openapi.json",
        "recommended_operations": [
            "/omni/search",
            "/omni/resolve",
            "/omni/compare",
            "/source/page",
            "/source/search",
            "/sources",
        ],
    }


@app.get("/health", tags=["System"])
async def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "version": APP_VERSION,
        "uptime_seconds": round(time.monotonic() - SERVICE_STARTED_AT, 2),
        "cache": await cache.stats(),
        "circuit_breakers": {
            host: {
                "state": breaker.state.value,
                "failures": breaker.failures,
            }
            for host, breaker in network.breakers.items()
        },
        "timestamp": utc_now_iso(),
    }


@app.get("/sources", tags=["Sources"])
async def list_sources(
    canon: Optional[str] = None,
    kind: Optional[str] = None,
    language: Optional[str] = None,
    live_only: bool = False,
    tag: Optional[str] = None,
):
    sources = registry.list(
        canon=canon,
        kind=kind,
        language=language,
        live_only=live_only,
        tag=tag,
    )
    return {
        "ok": True,
        "sources": [source.public_dict() for source in sources],
        "count": len(sources),
        "meta": {
            "note": (
                "Registry membership and live reachability are separate. "
                "Use /sources/health to probe current source availability."
            )
        },
    }


@app.get("/sources/health", tags=["Sources"])
async def source_health(
    source_ids: Optional[str] = Query(
        default=None,
        max_length=2000,
        description="Comma-separated source IDs. Omit to probe core sources.",
    ),
):
    if source_ids:
        sources = [registry.get(item) for item in (parse_csv_param(source_ids) or [])]
    else:
        sources = omni._select_sources(scope="core")

    semaphore = asyncio.Semaphore(8)

    async def probe_one(source: SourceConfig):
        async with semaphore:
            try:
                return await adapter_for(source).probe()
            except Exception as exc:
                return {
                    "source_id": source.id,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }

    results = await asyncio.gather(*(probe_one(source) for source in sources))
    return {
        "ok": True,
        "results": results,
        "healthy": sum(1 for item in results if item.get("ok")),
        "unhealthy": sum(1 for item in results if not item.get("ok")),
        "timestamp": utc_now_iso(),
    }


# =============================================================================
# 17. GENERIC SOURCE ROUTES
# =============================================================================

@app.get("/source/search", tags=["Sources"])
async def source_search(
    source_id: str = Query(min_length=1, max_length=120),
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    limit: int = Query(default=10, ge=1, le=MAX_SOURCE_SEARCH_RESULTS),
):
    try:
        source = registry.get(source_id)
        hits = await adapter_for(source).search(q, limit=limit)
        return {
            "ok": True,
            "status": "ok" if hits else "search_no_match",
            "source": source.public_dict(),
            "query": normalize_query(q),
            "results": [hit.to_dict() for hit in hits],
        }
    except SourceNotFound as exc:
        return error_response(404, "source_not_found", str(exc))
    except BadSourceQuery as exc:
        return error_response(400, "bad_query", str(exc), source_id=source_id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source_id, retryable=True)


@app.get("/source/page", tags=["Sources"])
async def source_page(
    source_id: str = Query(min_length=1, max_length=120),
    page: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    max_chars: int = Query(default=ACTION_DEFAULT_MAX_TEXT_CHARS, ge=2_000, le=ACTION_ABSOLUTE_MAX_TEXT_CHARS),
    allow_archive_fallback: bool = False,
):
    try:
        source = registry.get(source_id)
        payload = await adapter_for(source).fetch_page(
            page,
            max_chars=action_text_budget(max_chars),
            allow_archive_fallback=allow_archive_fallback,
        )
        return {"ok": True, "page": action_page_payload(payload)}
    except SourceNotFound as exc:
        return error_response(404, "source_not_found", str(exc))
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source_id)
    except BadSourceQuery as exc:
        return error_response(400, "bad_query", str(exc), source_id=source_id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source_id, retryable=True)


@app.get("/source/recent", tags=["Sources"])
async def source_recent(
    source_id: str = Query(min_length=1, max_length=120),
    limit: int = Query(default=10, ge=1, le=50),
):
    try:
        source = registry.get(source_id)
        hits = await adapter_for(source).recent(limit=limit)
        return {
            "ok": True,
            "source": source.public_dict(),
            "results": [hit.to_dict() for hit in hits],
        }
    except SourceNotFound as exc:
        return error_response(404, "source_not_found", str(exc))
    except BadSourceQuery as exc:
        return error_response(400, "unsupported_operation", str(exc), source_id=source_id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source_id, retryable=True)


# =============================================================================
# 18. OMNI ROUTES
# =============================================================================

def parse_csv_param(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    if len(value) > 2000:
        raise BadSourceQuery("Comma-separated parameter is too long.")
    items: List[str] = []
    for item in value.split(","):
        normalized = item.strip()
        if normalized and normalized not in items:
            items.append(normalized)
    if len(items) > MAX_SOURCE_IDS_PER_REQUEST:
        raise BadSourceQuery(
            f"At most {MAX_SOURCE_IDS_PER_REQUEST} values may be requested at once."
        )
    return items or None


@app.get("/omni/search", tags=["Omni Lore"])
async def omni_search(
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    scope: str = Query(default="core", max_length=20),
    source_ids: Optional[str] = Query(default=None, max_length=2000),
    languages: Optional[str] = Query(default=None, max_length=2000),
    per_source_limit: int = Query(default=6, ge=1, le=15),
    total_limit: int = Query(default=40, ge=1, le=100),
):
    try:
        return await omni.search(
            q,
            scope=scope,
            source_ids=parse_csv_param(source_ids),
            languages=parse_csv_param(languages),
            per_source_limit=per_source_limit,
            total_limit=total_limit,
        )
    except (BadSourceQuery, SourceNotFound) as exc:
        return error_response(400, "bad_query", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.get("/omni/resolve", tags=["Omni Lore"])
async def omni_resolve(
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    scope: str = Query(default="core", max_length=20),
    source_ids: Optional[str] = Query(default=None, max_length=2000),
    max_chars: int = Query(default=ACTION_DEFAULT_MAX_TEXT_CHARS, ge=2_000, le=ACTION_ABSOLUTE_MAX_TEXT_CHARS),
    allow_archive_fallback: bool = False,
):
    try:
        result = await omni.resolve_and_fetch(
            q,
            scope=scope,
            source_ids=parse_csv_param(source_ids),
            max_chars=action_text_budget(max_chars),
            allow_archive_fallback=allow_archive_fallback,
        )
        page_data = result.get("page")
        if isinstance(page_data, dict):
            # Reconstruct only to reuse the one bounded serialization path.
            result["page"] = action_page_payload(PagePayload(**{
                key: page_data[key]
                for key in PagePayload.__dataclass_fields__
                if key in page_data
            }))
        return result
    except PageNotFound as exc:
        return error_response(404, "no_match", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "candidate_fetch_failed", str(exc), retryable=True)
    except (BadSourceQuery, SourceNotFound) as exc:
        return error_response(400, "bad_query", str(exc))


@app.get("/omni/compare", tags=["Omni Lore"])
async def omni_compare(
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    scope: str = Query(default="core", max_length=20),
    source_ids: Optional[str] = Query(default=None, max_length=2000),
    max_chars_per_source: int = Query(default=16000, ge=4_000, le=40_000),
    allow_archive_fallback: bool = False,
):
    try:
        return await omni.compare(
            q,
            source_ids=parse_csv_param(source_ids),
            scope=scope,
            max_chars_per_source=max_chars_per_source,
            allow_archive_fallback=allow_archive_fallback,
        )
    except (BadSourceQuery, SourceNotFound) as exc:
        return error_response(400, "bad_query", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.get("/omni/recent", tags=["Omni Lore"])
async def omni_recent(
    scope: str = Query(default="core", max_length=20),
    source_ids: Optional[str] = Query(default=None, max_length=2000),
    per_source_limit: int = Query(default=8, ge=1, le=20),
    total_limit: int = Query(default=40, ge=1, le=100),
):
    try:
        return await omni.recent_across_sources(
            scope=scope,
            source_ids=parse_csv_param(source_ids),
            per_source_limit=per_source_limit,
            total_limit=total_limit,
        )
    except (BadSourceQuery, SourceNotFound) as exc:
        return error_response(400, "bad_query", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.get("/omni/graph", tags=["Omni Lore"])
async def omni_graph(
    source_id: str = Query(min_length=1, max_length=120),
    page: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    depth: int = Query(default=1, ge=1, le=2),
    max_nodes: int = Query(default=25, ge=2, le=60),
):
    try:
        return await omni.build_link_graph(
            source_id,
            page,
            depth=depth,
            max_nodes=max_nodes,
        )
    except SourceNotFound as exc:
        return error_response(404, "source_not_found", str(exc))
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source_id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source_id, retryable=True)


# =============================================================================
# 19. LIMINAL ARCHIVES ROUTES
# =============================================================================

@app.get("/archives/liminal/search", tags=["Liminal Archives"])
async def search_liminal_archives(
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    limit: int = Query(default=10, ge=1, le=MAX_SOURCE_SEARCH_RESULTS),
):
    """
    Search Liminal Archives live first, then its configured archival domains.
    Archived results are clearly marked archived=true.
    """
    try:
        source = registry.get("liminal-archives")
        hits = await adapter_for(source).search(q, limit=limit)
        return {
            "ok": True,
            "status": "ok" if hits else "search_no_match",
            "source": source.public_dict(),
            "query": normalize_query(q),
            "results": [hit.to_dict() for hit in hits],
        }
    except SourceUnavailable as exc:
        return error_response(
            503,
            "liminal_source_unavailable",
            str(exc),
            source_id="liminal-archives",
            retryable=True,
        )


@app.get("/archives/liminal", tags=["Liminal Archives"])
async def get_liminal_archives(
    page: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    max_chars: int = Query(default=ACTION_DEFAULT_MAX_TEXT_CHARS, ge=2_000, le=ACTION_ABSOLUTE_MAX_TEXT_CHARS),
    allow_archive_fallback: bool = False,
):
    """
    Backward-compatible route with corrected live Wikidot resolution and
    explicit Wayback fallback.
    """
    try:
        source = registry.get("liminal-archives")
        payload = await adapter_for(source).fetch_page(
            page,
            max_chars=action_text_budget(max_chars),
            allow_archive_fallback=allow_archive_fallback,
        )
        return {"ok": True, "page": action_page_payload(payload)}
    except PageNotFound as exc:
        return error_response(
            404,
            "liminal_page_not_found",
            str(exc),
            source_id="liminal-archives",
        )
    except SourceUnavailable as exc:
        return error_response(
            503,
            "liminal_source_unavailable",
            str(exc),
            source_id="liminal-archives",
            retryable=True,
        )


@app.get("/archives/liminal/recent", tags=["Liminal Archives"])
async def recent_liminal_archives(limit: int = Query(default=10, ge=1, le=50)):
    try:
        source = registry.get("liminal-archives")
        hits = await adapter_for(source).recent(limit=limit)
        return {
            "ok": True,
            "source": source.public_dict(),
            "results": [hit.to_dict() for hit in hits],
        }
    except SourceUnavailable as exc:
        return error_response(
            503,
            "liminal_source_unavailable",
            str(exc),
            source_id="liminal-archives",
            retryable=True,
        )


# =============================================================================
# 20. DYNAMIC FANDOM AND WIKIDOT ROUTES
# =============================================================================

@app.get("/dynamic/fandom/search", tags=["Dynamic Sources"])
async def dynamic_fandom_search(
    site: str = Query(min_length=3, max_length=253),
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    limit: int = Query(default=10, ge=1, le=MAX_SOURCE_SEARCH_RESULTS),
):
    try:
        source = dynamic_fandom_source(site)
        hits = await MediaWikiAdapter(source).search(q, limit=limit)
        return {
            "ok": True,
            "status": "ok" if hits else "search_no_match",
            "source": source.public_dict(),
            "query": normalize_query(q),
            "results": [hit.to_dict() for hit in hits],
        }
    except (UnsafeTarget, BadSourceQuery) as exc:
        return error_response(400, "unsafe_or_invalid_target", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.get("/dynamic/fandom/page", tags=["Dynamic Sources"])
async def dynamic_fandom_page(
    site: str = Query(min_length=3, max_length=253),
    page: Optional[str] = Query(default=None, min_length=1, max_length=MAX_QUERY_CHARS),
    # Backward-compatible alias for the v20 Action schema. v21 documents
    # `page`, but an older configured GPT that sends `title` still works.
    title: Optional[str] = Query(default=None, min_length=1, max_length=MAX_QUERY_CHARS),
    max_chars: int = Query(default=ACTION_DEFAULT_MAX_TEXT_CHARS, ge=2_000, le=ACTION_ABSOLUTE_MAX_TEXT_CHARS),
):
    try:
        source = dynamic_fandom_source(site)
        locator = page or title
        if not locator:
            raise BadSourceQuery("A page title is required.")
        payload = await MediaWikiAdapter(source).fetch_page(locator, max_chars=action_text_budget(max_chars))
        return {"ok": True, "page": action_page_payload(payload)}
    except (UnsafeTarget, BadSourceQuery) as exc:
        return error_response(400, "unsafe_or_invalid_target", str(exc))
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.get("/dynamic/wikidot/search", tags=["Dynamic Sources"])
async def dynamic_wikidot_search(
    site: str = Query(min_length=3, max_length=253),
    q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    limit: int = Query(default=10, ge=1, le=MAX_SOURCE_SEARCH_RESULTS),
):
    try:
        source = dynamic_wikidot_source(site)
        hits = await WikidotAdapter(source).search(q, limit=limit)
        return {
            "ok": True,
            "status": "ok" if hits else "search_no_match",
            "source": source.public_dict(),
            "query": normalize_query(q),
            "results": [hit.to_dict() for hit in hits],
        }
    except (UnsafeTarget, BadSourceQuery) as exc:
        return error_response(400, "unsafe_or_invalid_target", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.get("/dynamic/wikidot/page", tags=["Dynamic Sources"])
async def dynamic_wikidot_page(
    site: str = Query(min_length=3, max_length=253),
    page: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
    max_chars: int = Query(default=ACTION_DEFAULT_MAX_TEXT_CHARS, ge=2_000, le=ACTION_ABSOLUTE_MAX_TEXT_CHARS),
    allow_archive_fallback: bool = False,
):
    try:
        source = dynamic_wikidot_source(site)
        payload = await WikidotAdapter(source).fetch_page(
            page,
            max_chars=action_text_budget(max_chars),
            allow_archive_fallback=allow_archive_fallback,
        )
        return {"ok": True, "page": action_page_payload(payload)}
    except (UnsafeTarget, BadSourceQuery) as exc:
        return error_response(400, "unsafe_or_invalid_target", str(exc))
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


# =============================================================================
# 21. SUPPORTED WIKI URL READER
# =============================================================================

@app.get("/url/read", tags=["Dynamic Sources"])
async def read_supported_wiki_url(
    url: str = Query(min_length=8, max_length=2048),
    max_chars: int = Query(default=ACTION_DEFAULT_MAX_TEXT_CHARS, ge=2_000, le=ACTION_ABSOLUTE_MAX_TEXT_CHARS),
):
    """
    Read a user-supplied Fandom or Wikidot URL without needing a pre-registered
    source ID. The host is restricted to supported public wiki domains.
    """
    try:
        safe_url = validate_public_http_url(url)
        parsed = urlparse(safe_url)
        host = parsed.hostname or ""

        if host in {"web.archive.org", "archive.org"}:
            return error_response(
                400,
                "unsupported_source",
                "Direct archive URLs are not read as live pages. Use the source-specific archive fallback instead.",
            )

        if host.endswith(".fandom.com"):
            # Try to derive article title and use MediaWiki API.
            path_parts = [part for part in parsed.path.split("/") if part]
            if "wiki" in path_parts:
                index = path_parts.index("wiki")
                title = unquote("/".join(path_parts[index + 1 :])).replace("_", " ")
            else:
                title = unquote(path_parts[-1] if path_parts else "").replace("_", " ")
            source = dynamic_fandom_source(host)
            payload = await MediaWikiAdapter(source).fetch_page(title, max_chars=action_text_budget(max_chars))
            return {"ok": True, "page": action_page_payload(payload)}

        if host.endswith(".wikidot.com"):
            page = unquote(parsed.path.strip("/"))
            source = dynamic_wikidot_source(host)
            payload = await WikidotAdapter(source).fetch_page(
                page,
                max_chars=action_text_budget(max_chars),
                allow_archive_fallback=False,
            )
            return {"ok": True, "page": action_page_payload(payload)}

        # Explicit configured host fallback.
        response, diagnostics = await network.get(safe_url, retries=1, allow_404=True)
        if response is None or response.status_code == 404:
            raise PageNotFound(f"URL not found: {safe_url}")
        doc = sanitize_html_document(
            response.text,
            page_url=str(response.url),
            max_chars=action_text_budget(max_chars),
        )
        payload = PagePayload(
            source_id="dynamic-url",
            source_name=host,
            canon="Unclassified external Backrooms source",
            title=doc.title,
            url=str(response.url),
            text=doc.text,
            headings=doc.headings,
            links=doc.links,
            image_urls=doc.image_urls,
            analysis=LoreAnalyzer.analyze(doc),
            retrieved_at=utc_now_iso(),
            archived=False,
            upstream_diagnostics=[asdict(d) for d in diagnostics],
        )
        return {"ok": True, "page": action_page_payload(payload)}
    except UnsafeTarget as exc:
        return error_response(400, "unsafe_target", str(exc))
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc))
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), retryable=True)


# =============================================================================
# 22. LEGACY COMPATIBILITY ROUTES
# =============================================================================

@app.get("/fandom/search", tags=["Legacy Compatibility"])
async def legacy_fandom_search(q: str = Query(min_length=1, max_length=MAX_QUERY_CHARS)):
    source = registry.get("fandom-main")
    try:
        hits = await MediaWikiAdapter(source).search(q, limit=10)
        # Preserve a MediaWiki-like query.search shape for older GPT schemas.
        return {
            "query": {
                "search": [
                    {
                        "title": hit.title,
                        "pageid": hit.page_id,
                        "snippet": hit.snippet,
                        "url": hit.url,
                    }
                    for hit in hits
                ]
            },
            "_gateway_meta": {
                "source_id": source.id,
                "version": APP_VERSION,
            },
        }
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source.id, retryable=True)


@app.get("/fandom/page", tags=["Legacy Compatibility"])
async def legacy_fandom_page(title: str = Query(min_length=1, max_length=MAX_QUERY_CHARS)):
    source = registry.get("fandom-main")
    try:
        payload = await MediaWikiAdapter(source).fetch_page(title)
        return legacy_content_wrapper(payload)
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source.id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source.id, retryable=True)


@app.get("/wikidot/page", tags=["Legacy Compatibility"])
async def legacy_wikidot_page(url: str = Query(min_length=1, max_length=MAX_QUERY_CHARS)):
    source = registry.get("wikidot-main")
    try:
        payload = await WikidotAdapter(source).fetch_page(url)
        return legacy_content_wrapper(payload)
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source.id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source.id, retryable=True)


@app.get("/wikidot/international", tags=["Legacy Compatibility"])
async def legacy_international_wikidot(
    lang: str = Query(min_length=2, max_length=10),
    page: str = Query(min_length=1, max_length=MAX_QUERY_CHARS),
):
    language_map = {
        "ru": "wikidot-ru",
        "cn": "wikidot-cn",
        "zh": "wikidot-cn",
        "es": "wikidot-es",
        "fr": "wikidot-fr",
        "de": "wikidot-de",
        "it": "wikidot-it",
        "pl": "wikidot-pl",
        "pt": "wikidot-ptbr",
        "pt-br": "wikidot-ptbr",
        "jp": "wikidot-jp",
        "ja": "wikidot-jp",
        "ko": "wikidot-ko",
    }
    source_id = language_map.get(lang.casefold())
    if not source_id:
        return error_response(
            400,
            "unsupported_language",
            f"Unsupported international branch code '{lang}'.",
            details={"supported": sorted(language_map)},
        )
    source = registry.get(source_id)
    try:
        payload = await WikidotAdapter(source).fetch_page(page)
        return legacy_content_wrapper(payload)
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source.id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source.id, retryable=True)


@app.get("/wikidot/freewriting", tags=["Legacy Compatibility"])
async def legacy_freewriting(page: str = Query(min_length=1, max_length=MAX_QUERY_CHARS)):
    source = registry.get("freewriting-fandom")
    try:
        payload = await MediaWikiAdapter(source).fetch_page(page)
        return legacy_content_wrapper(payload)
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source.id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source.id, retryable=True)


@app.get("/cinematic/kanepixels", tags=["Legacy Compatibility"])
async def legacy_kane_pixels(topic: str = Query(min_length=1, max_length=MAX_QUERY_CHARS)):
    source = registry.get("kanepixels-fandom")
    try:
        payload = await MediaWikiAdapter(source).fetch_page(topic)
        return legacy_content_wrapper(payload)
    except PageNotFound as exc:
        return error_response(404, "page_not_found", str(exc), source_id=source.id)
    except SourceUnavailable as exc:
        return error_response(503, "source_unavailable", str(exc), source_id=source.id, retryable=True)


def legacy_content_wrapper(payload: PagePayload) -> dict:
    analysis = payload.analysis
    content = (
        f"[SOURCE: {payload.source_name}]\n"
        f"[CANON: {payload.canon}]\n"
        f"[URL: {payload.url}]\n"
        f"[ARCHIVED: {str(payload.archived).lower()}]\n"
        f"[RETRIEVED: {payload.retrieved_at}]\n\n"
        f"[EXTRACTIVE SUMMARY]\n{analysis.get('summary', '')}\n\n"
        f"[HEURISTIC HAZARD SIGNAL - NOT OFFICIAL CLASSIFICATION]\n"
        f"{json.dumps(analysis.get('heuristic_hazard_signal', {}), ensure_ascii=False)}\n\n"
        f"[RAW ARCHIVAL TEXT]\n{payload.text}"
    )
    return {
        "content": content,
        "provenance": payload.to_dict(include_text=False)["provenance"],
        "analysis": analysis,
    }


# =============================================================================
# 23. DEBUG / SELF-TEST SUPPORT
# =============================================================================

@app.get("/selftest", tags=["System"])
async def selftest():
    """
    Non-destructive local diagnostic.  It intentionally never sends upstream
    requests: public source probes belong to the separately bounded
    ``/sources/health`` endpoint.
    """
    local_checks = {
        "slug_level_0": generate_slug_candidates("Level 0")[:8],
        "slug_baby_food": generate_slug_candidates("Baby Food")[:8],
        "title_similarity_exact": title_similarity("Level 0", "Level 0"),
        "title_similarity_related": round(title_similarity("Baby Food", "Baby-Food"), 4),
        "source_ids": registry.all_ids(),
    }

    return {
        "ok": True,
        "version": APP_VERSION,
        "local_checks": local_checks,
        "live_probes": [],
        "note": "No live probes were executed. Use /sources/health for bounded live source status.",
        "timestamp": utc_now_iso(),
    }


# =============================================================================
# 24. GLOBAL EXCEPTION HANDLERS
# =============================================================================

@app.exception_handler(SourceNotFound)
async def source_not_found_handler(request: Request, exc: SourceNotFound):
    return error_response(404, "source_not_found", str(exc))


@app.exception_handler(PageNotFound)
async def page_not_found_handler(request: Request, exc: PageNotFound):
    return error_response(404, "page_not_found", str(exc))


@app.exception_handler(SourceUnavailable)
async def source_unavailable_handler(request: Request, exc: SourceUnavailable):
    return error_response(503, "source_unavailable", str(exc), retryable=True)


@app.exception_handler(UnsafeTarget)
async def unsafe_target_handler(request: Request, exc: UnsafeTarget):
    return error_response(400, "unsafe_target", str(exc))


@app.exception_handler(BadSourceQuery)
async def bad_query_handler(request: Request, exc: BadSourceQuery):
    return error_response(400, "bad_query", str(exc))


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    issues = [
        {
            "location": ".".join(str(part) for part in error.get("loc", [])[1:]),
            "message": error.get("msg", "Invalid value"),
            "type": error.get("type", "validation_error"),
        }
        for error in exc.errors()[:12]
    ]
    return error_response(
        422,
        "validation_failure",
        "Request validation failed.",
        details={"issues": issues},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    codes = {
        401: "auth_required",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "request_too_large",
        422: "validation_failure",
        429: "rate_limited",
        503: "service_unavailable",
    }
    detail = exc.detail if isinstance(exc.detail, str) else "Request could not be completed."
    return error_response(
        exc.status_code,
        codes.get(exc.status_code, "request_error"),
        detail,
        retryable=exc.status_code in {429, 503},
        headers=exc.headers,
    )


# =============================================================================
# END OF BACKROOMSGPT OMNI-LORE GATEWAY
# =============================================================================


# =============================================================================
# 25. ATLAS PLATFORM EXTENSIONS
# =============================================================================
from atlas.routes import install_atlas_platform

atlas_platform = install_atlas_platform(
    app=app,
    registry=registry,
    omni=omni,
    adapter_for=adapter_for,
    network=network,
    utc_now_iso=utc_now_iso,
    validate_discovery_url=validate_discovery_seed_url,
)

# Add the ASGI body cap after all decorator middleware registrations so it is
# the outermost application guard: an oversized request is rejected before
# authentication, route middleware, or Pydantic can read it.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
