from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, List


class MediaResearch:
    COMMONS_API = "https://commons.wikimedia.org/w/api.php"
    COMMONS_USER_AGENT = (
        "BackroomsGPT-MediaResearch/21.0 "
        "(https://backrooms-api.onrender.com/privacy; contact via Builder Profile)"
    )

    def __init__(self, network):
        self.network = network

    @staticmethod
    def _plain(value: str | None) -> str:
        if not value:
            return ""
        value = re.sub(r"<[^>]+>", " ", value)
        return re.sub(r"\s+", " ", html.unescape(value)).strip()

    @classmethod
    def _metadata_value(cls, metadata: Dict[str, Any], key: str) -> str:
        return cls._plain((metadata.get(key) or {}).get("value"))

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", (value or "").casefold())
            if len(token) > 2
        }

    @classmethod
    def _relevance_score(
        cls, query: str, title: str, description: str, categories: str
    ) -> float:
        query_tokens = cls._tokens(query)
        if not query_tokens:
            return 0.0
        title_tokens = cls._tokens(title)
        body_tokens = cls._tokens(f"{description} {categories}")
        title_overlap = len(query_tokens & title_tokens) / len(query_tokens)
        body_overlap = len(query_tokens & body_tokens) / len(query_tokens)
        phrase_bonus = (
            0.35
            if query.casefold() in f"{title} {description}".casefold()
            else 0.0
        )
        return round(
            (title_overlap * 0.65) + (body_overlap * 0.35) + phrase_bonus,
            4,
        )

    @staticmethod
    def _usable_image(image_info: Dict[str, Any]) -> bool:
        mime = (image_info.get("mime") or "").casefold()
        if not mime.startswith("image/"):
            return False
        if mime in {"image/vnd.djvu", "image/x.djvu"}:
            return False
        return bool(image_info.get("url"))

    async def _commons_request(self, params: Dict[str, Any]):
        """Query Commons with deterministic transfer encoding and bounded retries.

        The shared gateway deliberately requests identity encoding after observing
        compressed upstream payloads that were not decoded reliably on Render.
        Commons must not override that safety choice with gzip.
        """
        last_exception = None
        combined_diagnostics = []

        for attempt in range(2):
            try:
                response, diagnostics = await self.network.request(
                    "GET",
                    self.COMMONS_API,
                    params=params,
                    retries=3,
                    json_preferred=True,
                    extra_headers={
                        "User-Agent": self.COMMONS_USER_AGENT,
                        "Api-User-Agent": self.COMMONS_USER_AGENT,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    allow_404=False,
                    max_response_bytes=2_000_000,
                )
                combined_diagnostics.extend(diagnostics)
                if response is not None and response.status_code == 200:
                    return response, combined_diagnostics
            except Exception as exc:
                last_exception = exc
                combined_diagnostics.append(
                    f"{type(exc).__name__}: {exc}"
                )

            if attempt == 0:
                await asyncio.sleep(0.4)

        if last_exception is not None:
            raise last_exception
        return None, combined_diagnostics

    async def search_commons(self, query: str, limit: int = 12):
        limit = max(1, min(int(limit), 30))
        query = re.sub(r"\s+", " ", (query or "")).strip()
        if not query:
            return {
                "ok": False,
                "query": query,
                "results": [],
                "error": {
                    "code": "validation_failure",
                    "message": "query cannot be empty",
                    "retryable": False,
                },
            }

        variations = [
            query,
            f'"{query}"',
            f"{query} photograph",
            " ".join(token for token in query.split() if token.casefold() not in {
                "empty", "preferred", "visible", "no", "people"
            }),
        ]
        seen_variations = []
        for item in variations:
            item = re.sub(r"\s+", " ", item).strip()
            if item and item not in seen_variations:
                seen_variations.append(item)

        merged: Dict[int, Dict[str, Any]] = {}
        diagnostics: List[str] = []
        successful_requests = 0

        for search_query in seen_variations:
            params = {
                "action": "query",
                "generator": "search",
                "gsrsearch": search_query,
                "gsrnamespace": 6,
                "gsrlimit": min(max(limit * 3, 12), 50),
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|mime|size",
                "format": "json",
                "formatversion": 2,
                "maxlag": 5,
            }

            try:
                response, request_diagnostics = await self._commons_request(params)
                diagnostics.extend(str(item) for item in request_diagnostics)
            except Exception as exc:
                diagnostics.append(f"{type(exc).__name__}: {exc}")
                continue

            if not response or response.status_code != 200:
                continue

            try:
                data = response.json()
            except Exception as exc:
                diagnostics.append(
                    f"Invalid JSON: {type(exc).__name__}: {exc}"
                )
                continue

            api_error = data.get("error")
            if api_error:
                diagnostics.append(
                    f"Commons API error {api_error.get('code')}: "
                    f"{api_error.get('info', '')}"
                )
                if api_error.get("code") == "maxlag":
                    await asyncio.sleep(0.6)
                continue

            successful_requests += 1
            pages = data.get("query", {}).get("pages", [])
            if isinstance(pages, dict):
                pages = list(pages.values())

            for page in pages:
                pageid = page.get("pageid")
                if pageid is None:
                    continue

                image_info = (page.get("imageinfo") or [{}])[0]
                if not self._usable_image(image_info):
                    continue

                metadata = image_info.get("extmetadata", {})
                description = self._metadata_value(
                    metadata, "ImageDescription"
                )
                categories = self._metadata_value(metadata, "Categories")
                title = page.get("title") or ""
                relevance = self._relevance_score(
                    query, title, description, categories
                )
                if relevance < 0.20:
                    continue

                candidate = {
                    "title": title,
                    "pageid": pageid,
                    "image_url": image_info.get("url"),
                    "description_url": image_info.get("descriptionurl"),
                    "mime": image_info.get("mime"),
                    "width": image_info.get("width"),
                    "height": image_info.get("height"),
                    "description": description,
                    "author": self._metadata_value(metadata, "Artist"),
                    "license_short_name": self._metadata_value(
                        metadata, "LicenseShortName"
                    ),
                    "license_url": self._metadata_value(
                        metadata, "LicenseUrl"
                    ),
                    "usage_terms": self._metadata_value(
                        metadata, "UsageTerms"
                    ),
                    "credit": self._metadata_value(metadata, "Credit"),
                    "attribution_required": self._metadata_value(
                        metadata, "AttributionRequired"
                    ),
                    "restrictions": self._metadata_value(
                        metadata, "Restrictions"
                    ),
                    "relevance_score": relevance,
                    "search_variation": search_query,
                }
                previous = merged.get(pageid)
                if (
                    previous is None
                    or relevance > previous["relevance_score"]
                ):
                    merged[pageid] = candidate

            strong = [
                item
                for item in merged.values()
                if item["relevance_score"] >= 0.55
            ]
            if len(strong) >= limit:
                break

        results = sorted(
            merged.values(),
            key=lambda item: (
                item["relevance_score"],
                item.get("width") or 0,
            ),
            reverse=True,
        )[:limit]

        if not results and successful_requests == 0:
            return {
                "ok": False,
                "query": query,
                "source": "Wikimedia Commons",
                "result_count": 0,
                "results": [],
                "diagnostics": diagnostics[-12:],
                "error": {
                    "code": "source_unavailable",
                    "message": "Wikimedia Commons could not be queried reliably.",
                    "retryable": True,
                },
            }

        return {
            "ok": True,
            "query": query,
            "source": "Wikimedia Commons",
            "result_count": len(results),
            "results": results,
            "diagnostics": diagnostics[-12:],
            "status": "ok" if results else "search_no_match",
            "note": (
                "Candidates are relevance-ranked and filtered to image MIME types. "
                "Verify each file description page, authorship, license, attribution, "
                "and modification requirements before publication."
            ),
        }

    def catalog(self):
        return {
            "ok": True,
            "sources": [
                {
                    "id": "wikimedia-commons",
                    "name": "Wikimedia Commons",
                    "live_search": True,
                    "license_metadata": True,
                },
                {
                    "id": "manual-render",
                    "name": "Manual render workflow",
                    "live_search": False,
                    "notes": (
                        "Use Blender, Roblox Studio, photography, or other "
                        "non-AI manual creation."
                    ),
                },
                {
                    "id": "original-photo",
                    "name": "Original photography",
                    "live_search": False,
                    "notes": (
                        "Check people, trademarks, private property, and local "
                        "photography restrictions."
                    ),
                },
            ],
        }
