"""Offline regressions for v21 retrieval identity and Atlas graph behavior.

These tests deliberately use small adapters and stores.  They must never make a
network request: the intent is to keep the Liminal and resolver guarantees
stable even when upstream sites are unavailable during local development.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import main
from atlas.evals import EvalSuite


def page_payload(
    *,
    source: main.SourceConfig,
    title: str,
    url: str,
    archived: bool = False,
    links: list[dict] | None = None,
) -> main.PagePayload:
    return main.PagePayload(
        source_id=source.id,
        source_name=source.name,
        canon=source.canon,
        title=title,
        url=url,
        text="Offline fixture text.",
        headings=[],
        links=links or [],
        image_urls=[],
        analysis={},
        retrieved_at="2026-07-09T00:00:00+00:00",
        archived=archived,
    )


class FixtureRegistry:
    def __init__(self, *sources: main.SourceConfig) -> None:
        self.sources = {source.id: source for source in sources}

    def get(self, source_id: str) -> main.SourceConfig:
        return self.sources[source_id]

    def all_ids(self) -> list[str]:
        return sorted(self.sources)


class MemoryEvalStore:
    def save_eval(self, suite: str, results: list[dict]) -> dict:
        return {
            "run_id": "offline-v21",
            "passed": sum(1 for result in results if result["passed"]),
            "failed": sum(1 for result in results if not result["passed"]),
            "results": results,
        }


class IdentityRegressionTests(unittest.TestCase):
    def test_live_and_archive_identity_reject_near_matches(self) -> None:
        level_cases = [
            ("Level 1", "https://liminal-archives.wikidot.com/level-1"),
            ("Level 10.1", "https://liminal-archives.wikidot.com/level-10-1"),
            ("Level 100", "https://liminal-archives.wikidot.com/level-100"),
            ("Corn Maze", "https://liminal-archives.wikidot.com/level-10-1"),
            ("Help on searching", "https://liminal-archives.wikidot.com/help:on-searching"),
        ]
        for title, url in level_cases:
            with self.subTest(query="Level 0", title=title):
                self.assertFalse(main.live_candidate_is_acceptable("Level 0", title, url))
                self.assertFalse(main.archive_candidate_is_acceptable("Level 0", title, url))

        review_url = "https://liminal-archives.wikidot.com/baby-food-review"
        self.assertFalse(
            main.live_candidate_is_acceptable("Baby Food", "Baby Food Review", review_url)
        )
        self.assertFalse(
            main.archive_candidate_is_acceptable("Baby Food", "Baby Food Review", review_url)
        )

    def test_generic_display_title_can_use_exact_canonical_slug(self) -> None:
        live_url = "https://liminal-archives.wikidot.com/baby-food"
        archive_url = (
            "https://web.archive.org/web/20200101000000id_/"
            "http://liminal-archives.wikidot.com/baby-food"
        )
        self.assertTrue(main.live_candidate_is_acceptable("Baby Food", "Liminal Archives", live_url))
        self.assertTrue(main.archive_candidate_is_acceptable("Baby Food", "Liminal Archives", archive_url))

    def test_resolver_fetches_the_search_hit_not_its_generic_display_title(self) -> None:
        source = main.SourceConfig(
            id="liminal-archives",
            name="Liminal Archives",
            kind=main.SourceKind.WIKIDOT,
            canon="Liminal Archives",
            priority=1,
            base_urls=("https://liminal-archives.wikidot.com",),
        )
        registry = FixtureRegistry(source)

        class Adapter:
            def __init__(self) -> None:
                self.received_hit: main.SearchHit | None = None

            async def search(self, query: str, limit: int = 10):
                return [
                    main.SearchHit(
                        source_id=source.id,
                        source_name=source.name,
                        canon=source.canon,
                        title="Liminal Archives",
                        url="https://liminal-archives.wikidot.com/baby-food",
                        score=1.0,
                    )
                ]

            async def fetch_hit(self, hit: main.SearchHit, **_kwargs):
                self.received_hit = hit
                return page_payload(
                    source=source,
                    title="Baby Food",
                    url="https://liminal-archives.wikidot.com/baby-food",
                )

        adapter = Adapter()
        with patch.object(main, "adapter_for", return_value=adapter):
            result = asyncio.run(
                main.OmniLoreEngine(registry).resolve_and_fetch(
                    "Baby Food", source_ids=[source.id]
                )
            )

        self.assertEqual(adapter.received_hit.url, "https://liminal-archives.wikidot.com/baby-food")
        self.assertEqual(result["page"]["url"], adapter.received_hit.url)

    def test_resolver_reports_source_unavailable_when_every_search_fails(self) -> None:
        source = main.SourceConfig(
            id="offline-source",
            name="Offline Source",
            kind=main.SourceKind.WIKIDOT,
            canon="Offline",
            priority=1,
            base_urls=("https://offline.wikidot.com",),
        )

        class OfflineAdapter:
            async def search(self, _query: str, limit: int = 10):
                raise main.SourceUnavailable("fixture source is offline")

        with patch.object(main, "adapter_for", return_value=OfflineAdapter()):
            with self.assertRaises(main.SourceUnavailable):
                asyncio.run(
                    main.OmniLoreEngine(FixtureRegistry(source)).resolve_and_fetch(
                        "Baby Food", source_ids=[source.id]
                    )
                )

    def test_resolver_skips_archive_hits_when_archive_fallback_is_disabled(self) -> None:
        source = main.SourceConfig(
            id="liminal-archives",
            name="Liminal Archives",
            kind=main.SourceKind.WIKIDOT,
            canon="Liminal Archives",
            priority=1,
            base_urls=("https://liminal-archives.wikidot.com",),
        )

        class ArchiveOnlyAdapter:
            def __init__(self):
                self.fetch_called = False

            async def search(self, _query: str, limit: int = 10):
                return [
                    main.SearchHit(
                        source_id=source.id,
                        source_name=source.name,
                        canon=source.canon,
                        title="Baby Food",
                        url="https://web.archive.org/web/20200101000000id_/http://liminal-archives.wikidot.com/baby-food",
                        archived=True,
                    )
                ]

            async def fetch_hit(self, *_args, **_kwargs):
                self.fetch_called = True
                raise AssertionError("archive hit must not be fetched")

        adapter = ArchiveOnlyAdapter()
        with patch.object(main, "adapter_for", return_value=adapter):
            with self.assertRaises(main.PageNotFound):
                asyncio.run(
                    main.OmniLoreEngine(FixtureRegistry(source)).resolve_and_fetch(
                        "Baby Food", source_ids=[source.id], allow_archive_fallback=False
                    )
                )
        self.assertFalse(adapter.fetch_called)

    def test_resolver_returns_page_not_found_for_identity_rejections(self) -> None:
        source = main.SourceConfig(
            id="fixture-source",
            name="Fixture",
            kind=main.SourceKind.WIKIDOT,
            canon="Fixture",
            priority=1,
            base_urls=("https://fixture.wikidot.com",),
        )

        class RejectedAdapter:
            async def search(self, _query: str, limit: int = 10):
                return [
                    main.SearchHit(
                        source_id=source.id,
                        source_name=source.name,
                        canon=source.canon,
                        title="Level 1",
                        url="https://fixture.wikidot.com/level-1",
                    )
                ]

            async def fetch_hit(self, *_args, **_kwargs):
                raise main.PageNotFound("fixture identity rejection")

        with patch.object(main, "adapter_for", return_value=RejectedAdapter()):
            with self.assertRaises(main.PageNotFound):
                asyncio.run(
                    main.OmniLoreEngine(FixtureRegistry(source)).resolve_and_fetch(
                        "Level 0", source_ids=[source.id]
                    )
                )

    def test_action_page_payload_caps_giant_auxiliary_values(self) -> None:
        source = main.SourceConfig(
            id="fixture-source",
            name="Fixture",
            kind=main.SourceKind.WIKIDOT,
            canon="Fixture",
            priority=1,
            base_urls=("https://fixture.wikidot.com",),
        )
        huge = "x" * 200_000
        payload = page_payload(source=source, title=huge, url="https://fixture.wikidot.com/" + huge)
        payload.headings = [huge]
        payload.links = [{"title": huge, "url": "https://fixture.wikidot.com/" + huge}]
        payload.image_urls = ["https://fixture.wikidot.com/" + huge]
        payload.analysis = {
            "summary": huge,
            "named_signals": {f"signal-{index}-{huge}": [huge] * 30 for index in range(30)},
            "section_extracts": {f"section-{index}-{huge}": huge for index in range(30)},
            "unexpected_unbounded_field": huge,
        }
        payload.upstream_diagnostics = [
            {"url": "https://fixture.wikidot.com/" + huge, "message": huge}
        ]
        result = main.action_page_payload(payload)
        self.assertLessEqual(len(result["title"]), 500)
        self.assertLessEqual(len(result["url"]), 2048)
        self.assertLessEqual(len(result["headings"][0]), 500)
        self.assertLessEqual(len(result["links"][0]["url"]), 1024)
        self.assertLessEqual(len(result["image_urls"][0]), 1024)
        self.assertLessEqual(len(result["analysis"]["summary"]), 3000)
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            64_000,
        )
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            64_000,
        )


class EvalRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.liminal = main.SourceConfig(
            id="liminal-archives",
            name="Liminal Archives",
            kind=main.SourceKind.WIKIDOT,
            canon="Liminal Archives",
            priority=1,
            base_urls=("https://liminal-archives.wikidot.com",),
        )
        self.wikidot = main.SourceConfig(
            id="wikidot-main",
            name="Wikidot",
            kind=main.SourceKind.WIKIDOT,
            canon="Wikidot",
            priority=1,
            base_urls=("https://backrooms-wiki.wikidot.com",),
        )
        self.fandom = main.SourceConfig(
            id="fandom-main",
            name="Fandom",
            kind=main.SourceKind.MEDIAWIKI,
            canon="Fandom",
            priority=1,
            api_url="https://backrooms.fandom.com/api.php",
        )
        self.registry = FixtureRegistry(self.liminal, self.wikidot, self.fandom)

    def _suite(self, *, compare_records: list[dict], level_page: main.PagePayload) -> EvalSuite:
        liminal = self.liminal

        class LiminalAdapter:
            async def search(self, query: str, limit: int = 5):
                if query != "Baby Food":
                    raise AssertionError(f"unexpected Liminal search query: {query!r}")
                return [
                    main.SearchHit(
                        source_id=liminal.id,
                        source_name=liminal.name,
                        canon=liminal.canon,
                        title="Liminal Archives",
                        url="https://liminal-archives.wikidot.com/baby-food",
                    )
                ]

            async def fetch_page(self, locator: str, **_kwargs):
                if locator == "baby-food":
                    return page_payload(
                        source=liminal,
                        title="Baby Food",
                        url="https://liminal-archives.wikidot.com/baby-food",
                    )
                if locator != "Level 0":
                    raise AssertionError(f"unexpected Liminal page locator: {locator!r}")
                return level_page

        class Omni:
            async def compare(self, *_args, **_kwargs):
                return {
                    "meta": {"payload_mode": "compact-comparison"},
                    "records": compare_records,
                }

        return EvalSuite(MemoryEvalStore(), Omni(), self.registry, lambda _source: LiminalAdapter())

    def test_eval_rejects_compact_records_that_failed(self) -> None:
        suite = self._suite(
            compare_records=[
                {"source_id": "wikidot-main", "ok": False},
                {"source_id": "fandom-main", "ok": False},
            ],
            level_page=page_payload(
                source=self.liminal,
                title="Level 0",
                url="https://liminal-archives.wikidot.com/level-0",
            ),
        )
        result = asyncio.run(suite.run_acceptance(live=True))
        records = {row["name"]: row for row in result["results"]}
        self.assertFalse(records["compact_canon_compare"]["passed"])
        self.assertFalse(result["ok"])

    def test_eval_rejects_every_known_wrong_liminal_page(self) -> None:
        compare_records = [
            {"source_id": "wikidot-main", "ok": True},
            {"source_id": "fandom-main", "ok": True},
        ]
        wrong_pages = [
            ("Level 1", "https://liminal-archives.wikidot.com/level-1"),
            ("Level 10.1", "https://liminal-archives.wikidot.com/level-10-1"),
            ("Level 100", "https://liminal-archives.wikidot.com/level-100"),
            ("Corn Maze", "https://liminal-archives.wikidot.com/corn-maze"),
            ("Help on searching", "https://liminal-archives.wikidot.com/help:on-searching"),
        ]
        for title, url in wrong_pages:
            with self.subTest(title=title):
                suite = self._suite(
                    compare_records=compare_records,
                    level_page=page_payload(source=self.liminal, title=title, url=url),
                )
                result = asyncio.run(suite.run_acceptance(live=True))
                records = {row["name"]: row for row in result["results"]}
                self.assertFalse(records["reject_wrong_liminal_archive_match"]["passed"])

    def test_compare_returns_page_not_found_when_all_sources_are_reachable_but_missing(self) -> None:
        class MissingAdapter:
            async def fetch_page(self, *_args, **_kwargs):
                raise main.PageNotFound("fixture missing page")

        registry = FixtureRegistry(self.wikidot, self.fandom)
        with patch.object(main, "adapter_for", return_value=MissingAdapter()):
            with self.assertRaises(main.PageNotFound):
                asyncio.run(
                    main.OmniLoreEngine(registry).compare(
                        "Level 0", source_ids=[self.wikidot.id, self.fandom.id]
                    )
                )


class GraphRegressionTests(unittest.TestCase):
    def test_live_graph_is_bounded_and_keeps_root_provenance(self) -> None:
        source = main.SourceConfig(
            id="wikidot-main",
            name="Wikidot",
            kind=main.SourceKind.WIKIDOT,
            canon="Wikidot",
            priority=1,
            base_urls=("https://backrooms-wiki.wikidot.com",),
        )
        root = page_payload(
            source=source,
            title="Root",
            url="https://backrooms-wiki.wikidot.com/root",
            links=[
                {
                    "title": "Child",
                    "url": "https://backrooms-wiki.wikidot.com/child",
                }
            ],
        )
        child = page_payload(
            source=source,
            title="Child",
            url="https://backrooms-wiki.wikidot.com/child",
        )

        class Adapter:
            def __init__(self) -> None:
                self.calls: list[tuple[str, bool]] = []

            async def fetch_page(self, locator: str, *, allow_archive_fallback: bool, **_kwargs):
                self.calls.append((locator, allow_archive_fallback))
                return root if locator == "Root" else child

        adapter = Adapter()
        with patch.object(main, "adapter_for", return_value=adapter):
            graph = asyncio.run(
                main.OmniLoreEngine(FixtureRegistry(source)).build_link_graph(
                    source.id, "Root", depth=999, max_nodes=999
                )
            )

        self.assertEqual(graph["meta"]["max_depth"], 2)
        self.assertLessEqual(graph["meta"]["node_count"], 60)
        self.assertTrue(adapter.calls)
        self.assertTrue(all(not allow_archive for _, allow_archive in adapter.calls))
        root_node = next(node for node in graph["nodes"] if node["url"] == root.url)
        self.assertEqual(root_node["source_id"], source.id)
        self.assertEqual(root_node["canon"], source.canon)
        self.assertFalse(root_node["archived"])
        self.assertEqual(graph["edges"][0]["source_id"], source.id)


class CacheErrorRegressionTests(unittest.TestCase):
    def test_wikidot_negative_cache_preserves_source_unavailable(self) -> None:
        source = main.SourceConfig(
            id="liminal-archives",
            name="Liminal Archives",
            kind=main.SourceKind.WIKIDOT,
            canon="Liminal Archives",
            priority=1,
            base_urls=("https://liminal-archives.wikidot.com",),
        )
        adapter = main.WikidotAdapter(source)

        async def exercise() -> None:
            await main.cache.set(
                "page:liminal-archives:cache-probe:8000:true",
                {
                    "_negative": True,
                    "message": "upstream unavailable",
                    "error_type": "SourceUnavailable",
                },
                negative=True,
            )
            with self.assertRaises(main.SourceUnavailable):
                await adapter.fetch_page("cache-probe", max_chars=8000)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
