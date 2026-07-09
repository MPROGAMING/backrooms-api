from __future__ import annotations

import html
import re
from typing import Any, Dict, List


class MediaResearch:
    COMMONS_API = "https://commons.wikimedia.org/w/api.php"
    COMMONS_USER_AGENT = (
        "BackroomsGPT-MediaResearch/20.1 "
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
    def _relevance_score(cls, query: str, title: str, description: str, categories: str) -> float:
        q = cls._tokens(query)
        if not q:
            return 0.0
        title_tokens = cls._tokens(title)
        body_tokens = cls._tokens(f"{description} {categories}")
        title_overlap = len(q & title_tokens) / len(q)
        body_overlap = len(q & body_tokens) / len(q)
        phrase_bonus = 0.35 if query.casefold() in f"{title} {description}".casefold() else 0.0
        return round((title_overlap * 0.65) + (body_overlap * 0.35) + phrase_bonus, 4)

    @staticmethod
    def _usable_image(ii: Dict[str, Any]) -> bool:
        mime = (ii.get("mime") or "").casefold()
        if not mime.startswith("image/"):
            return False
        if mime in {"image/vnd.djvu", "image/x.djvu"}:
            return False
        return bool(ii.get("url"))

    async def _commons_request(self, params: Dict[str, Any]):
        # Wikimedia requires a meaningful User-Agent with contact information.
        # Use the network request API directly so the generic client's random UA
        # cannot replace this service-specific identity.
        return await self.network.request(
            "GET",
            self.COMMONS_API,
            params=params,
            retries=2,
            json_preferred=True,
            extra_headers={
                "User-Agent": self.COMMONS_USER_AGENT,
                "Api-User-Agent": self.COMMONS_USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
            allow_404=False,
        )

    async def search_commons(self, query: str, limit: int = 12):
        limit = max(1, min(int(limit), 30))
        query = re.sub(r"\s+", " ", (query or "")).strip()
        if not query:
            return {
                "ok": False,
                "query": query,
                "results": [],
                "error": "query cannot be empty",
            }

        # Search sequentially, following Wikimedia API etiquette. The variations
        # improve recall while the local relevance filter removes books/PDFs and
        # unrelated search noise.
        variations = [query, f'"{query}"', f"{query} photograph"]
        seen_variations = []
        for item in variations:
            if item not in seen_variations:
                seen_variations.append(item)

        merged: Dict[int, Dict[str, Any]] = {}
        diagnostics: List[str] = []

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
                "origin": "*",
            }
            try:
                response, diag = await self._commons_request(params)
                diagnostics.extend(str(d) for d in diag)
            except Exception as exc:
                diagnostics.append(f"{type(exc).__name__}: {exc}")
                continue

            if not response or response.status_code != 200:
                continue

            try:
                data = response.json()
            except Exception as exc:
                diagnostics.append(f"Invalid JSON: {type(exc).__name__}: {exc}")
                continue

            pages = data.get("query", {}).get("pages", [])
            if isinstance(pages, dict):
                pages = list(pages.values())

            for page in pages:
                pageid = page.get("pageid")
                if pageid is None:
                    continue
                ii = (page.get("imageinfo") or [{}])[0]
                if not self._usable_image(ii):
                    continue

                meta = ii.get("extmetadata", {})
                description = self._metadata_value(meta, "ImageDescription")
                categories = self._metadata_value(meta, "Categories")
                title = page.get("title") or ""
                relevance = self._relevance_score(query, title, description, categories)

                # Keep only candidates with at least meaningful token overlap.
                if relevance < 0.20:
                    continue

                candidate = {
                    "title": title,
                    "pageid": pageid,
                    "image_url": ii.get("url"),
                    "description_url": ii.get("descriptionurl"),
                    "mime": ii.get("mime"),
                    "width": ii.get("width"),
                    "height": ii.get("height"),
                    "description": description,
                    "author": self._metadata_value(meta, "Artist"),
                    "license_short_name": self._metadata_value(meta, "LicenseShortName"),
                    "license_url": self._metadata_value(meta, "LicenseUrl"),
                    "usage_terms": self._metadata_value(meta, "UsageTerms"),
                    "credit": self._metadata_value(meta, "Credit"),
                    "attribution_required": self._metadata_value(meta, "AttributionRequired"),
                    "restrictions": self._metadata_value(meta, "Restrictions"),
                    "relevance_score": relevance,
                    "search_variation": search_query,
                }
                previous = merged.get(pageid)
                if previous is None or relevance > previous["relevance_score"]:
                    merged[pageid] = candidate

            # Stop early once enough strong candidates exist.
            strong = [item for item in merged.values() if item["relevance_score"] >= 0.55]
            if len(strong) >= limit:
                break

        results = sorted(
            merged.values(),
            key=lambda item: (item["relevance_score"], item.get("width") or 0),
            reverse=True,
        )[:limit]

        return {
            "ok": bool(results),
            "query": query,
            "source": "Wikimedia Commons",
            "result_count": len(results),
            "results": results,
            "diagnostics": diagnostics[-12:],
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
                    "notes": "Use Blender, Roblox Studio, photography, or other non-AI manual creation.",
                },
                {
                    "id": "original-photo",
                    "name": "Original photography",
                    "live_search": False,
                    "notes": "Check people, trademarks, private property, and local photography restrictions.",
                },
            ],
        }
