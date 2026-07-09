from __future__ import annotations

import asyncio
import hashlib
import re
from urllib.parse import urlparse, urlunparse

from .common import feature_hash_vector, now_iso, title_key


_GENERIC_SIGNAL_TAILS = {
    "a", "an", "the", "is", "it", "are", "of", "whose", "which", "that",
    "this", "has", "have", "was", "were", "can", "could", "count",
}

_MEDIA_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff",
    ".pdf", ".djvu",
)


class AtlasIndexer:
    def __init__(self, store, omni, registry, adapter_for):
        self.store = store
        self.omni = omni
        self.registry = registry
        self.adapter_for = adapter_for

    def _doc_from_page(self, page: dict):
        source_id = (
            page.get("source_id")
            or page.get("provenance", {}).get("source_id")
            or "unknown"
        )
        text = (page.get("text", "") or "")[:120000]
        title = (page.get("title", "Untitled") or "Untitled")[:500]
        url = self._canonical_url(page.get("url", ""))
        content_hash = hashlib.sha256(
            text.encode("utf-8", errors="ignore")
        ).hexdigest()
        doc_id = hashlib.sha256(
            f"{source_id}|{url or title}".encode()
        ).hexdigest()[:32]
        analysis = page.get("analysis", {})
        return {
            "doc_id": doc_id,
            "source_id": source_id,
            "canon": page.get("canon")
            or page.get("provenance", {}).get("canon"),
            "title": title,
            "url": url,
            "language": page.get("language", "en"),
            "text": text,
            "summary": analysis.get("summary", ""),
            "content_hash": content_hash,
            "vector": feature_hash_vector(
                title + "\n" + analysis.get("summary", "") + "\n" + text[:60000]
            ),
            "metadata": {
                "analysis": analysis,
                "links": page.get("links", []),
                "image_urls": page.get("image_urls", []),
                "provenance": page.get("provenance", {}),
                "resolved_via": page.get("resolved_via"),
            },
            "archived": page.get("archived", False),
            "fetched_at": page.get("retrieved_at", now_iso()),
        }

    @staticmethod
    def _canonical_url(value):
        parsed = urlparse(value or "")
        if not parsed.scheme or not parsed.netloc:
            return value or ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    @classmethod
    def _graph_link_allowed(cls, doc_url: str, target_url: str) -> bool:
        source = urlparse(doc_url or "")
        target = urlparse(target_url or "")
        if not target.scheme or not target.netloc:
            return False
        if source.hostname and target.hostname != source.hostname:
            return False

        path = (target.path or "").casefold()
        if path.endswith(_MEDIA_EXTENSIONS):
            return False
        if "/wiki/file:" in path or path.startswith("/file:"):
            return False
        return True

    @staticmethod
    def _signal_allowed(relation: str, value: str) -> bool:
        value = (value or "").strip()
        if not value:
            return False

        if relation == "mentions_level":
            match = re.fullmatch(r"Level\s+(.+)", value, flags=re.IGNORECASE)
            if not match:
                return False
            tail = match.group(1).strip()
            if tail.casefold() in _GENERIC_SIGNAL_TAILS:
                return False
            return any(ch.isdigit() for ch in tail) or (
                tail[:1].isupper() and len(tail) >= 3
            )

        if relation == "mentions_entity":
            match = re.fullmatch(r"Entity\s+(.+)", value, flags=re.IGNORECASE)
            if not match:
                return False
            tail = match.group(1).strip()
            if tail.casefold() in _GENERIC_SIGNAL_TAILS:
                return False
            return any(ch.isdigit() for ch in tail) or (
                tail[:1].isupper() and len(tail) >= 3
            )

        return True

    def _edges(self, doc, page):
        out = []
        seen = set()

        for link in page.get("links", [])[:300]:
            url = self._canonical_url(link.get("url", ""))
            if not self._graph_link_allowed(doc.get("url", ""), url):
                continue

            title = (link.get("title") or url.rsplit("/", 1)[-1])[:500]
            key = hashlib.sha256(
                f"{doc['source_id']}|{url or title}".encode()
            ).hexdigest()[:32]
            dedupe = ("links_to", key)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            out.append(
                {
                    "source_id": doc["source_id"],
                    "to_key": key,
                    "to_title": title,
                    "to_url": url,
                    "relation": "links_to",
                    "confidence": 1.0,
                }
            )

        signals = page.get("analysis", {}).get("named_signals", {})
        signal_specs = [
            ("mentions_level", signals.get("level_designations", [])),
            ("mentions_entity", signals.get("entity_designations", [])),
            ("mentions_group", signals.get("groups", [])),
        ]
        for relation, values in signal_specs:
            for value in values[:80]:
                if not self._signal_allowed(relation, value):
                    continue
                key = f"signal:{relation}:{title_key(value)}"
                dedupe = (relation, key)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                out.append(
                    {
                        "source_id": doc["source_id"],
                        "to_key": key,
                        "to_title": value,
                        "relation": relation,
                        "confidence": 0.8,
                    }
                )
        return out

    def index_page_payload(self, page: dict):
        doc = self._doc_from_page(page)
        edges = self._edges(doc, page)
        self.store.upsert_document_with_edges(doc, edges, snapshot=True)
        return doc

    async def ingest(
        self,
        query: str,
        scope: str = "core",
        source_ids=None,
        allow_archive_fallback: bool = False,
    ):
        query = (query or "").strip()
        if not query or len(query) > 500:
            raise ValueError("query must contain 1-500 characters")

        result = await self.omni.resolve_and_fetch(
            query,
            scope=scope,
            source_ids=source_ids,
            allow_archive_fallback=allow_archive_fallback,
        )
        doc = await asyncio.to_thread(self.index_page_payload, result["page"])
        return {
            "ok": True,
            "query": query,
            "indexed": {
                key: doc[key]
                for key in [
                    "doc_id",
                    "source_id",
                    "canon",
                    "title",
                    "url",
                    "content_hash",
                    "archived",
                ]
            },
            "alternatives": result.get("alternatives", [])[:5],
        }

    async def ingest_source_page(
        self,
        source_id: str,
        page: str,
        allow_archive_fallback: bool = False,
    ):
        if not (page or "").strip() or len(page) > 500:
            raise ValueError("page must contain 1-500 characters")
        source = self.registry.get(source_id)
        payload = (
            await self.adapter_for(source).fetch_page(
                page,
                max_chars=120000,
                allow_archive_fallback=allow_archive_fallback,
            )
        ).to_dict()
        doc = await asyncio.to_thread(self.index_page_payload, payload)
        return {
            "ok": True,
            "indexed": {
                key: doc[key]
                for key in [
                    "doc_id",
                    "source_id",
                    "canon",
                    "title",
                    "url",
                    "content_hash",
                    "archived",
                ]
            },
        }

    async def sync_recent(
        self,
        scope: str = "core",
        per_source_limit: int = 5,
        total_limit: int = 25,
    ):
        per_source_limit = max(1, min(int(per_source_limit), 10))
        total_limit = max(1, min(int(total_limit), 50))
        recent = await self.omni.recent_across_sources(
            scope=scope,
            per_source_limit=per_source_limit,
            total_limit=total_limit,
        )
        hits = recent.get("results", [])
        semaphore = asyncio.Semaphore(6)

        async def one(hit):
            async with semaphore:
                try:
                    return await self.ingest_source_page(
                        hit["source_id"], hit["title"]
                    )
                except Exception as exc:
                    return {
                        "ok": False,
                        "source_id": hit.get("source_id"),
                        "title": hit.get("title"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        results = await asyncio.gather(*(one(hit) for hit in hits))
        return {
            "ok": True,
            "attempted": len(hits),
            "indexed": sum(1 for result in results if result.get("ok")),
            "failed": sum(1 for result in results if not result.get("ok")),
            "results": results,
        }
