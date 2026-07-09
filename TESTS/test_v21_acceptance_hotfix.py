from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import main
from atlas.indexer import AtlasIndexer
from atlas.media import MediaResearch


class FixtureRegistry:
    def __init__(self, source):
        self.source = source

    def get(self, source_id):
        if source_id != self.source.id:
            raise KeyError(source_id)
        return self.source

    def all_ids(self):
        return [self.source.id]


class ResolverAcceptanceHotfixTests(unittest.TestCase):
    def setUp(self):
        self.source = main.SourceConfig(
            id="fixture",
            name="Fixture",
            kind=main.SourceKind.MEDIAWIKI,
            canon="Fixture",
            priority=1,
            api_url="https://example.fandom.com/api.php",
            page_url_template="https://example.fandom.com/wiki/{title}",
        )

    def test_resolver_rejects_baby_partygoers_for_baby_food(self):
        source = self.source

        class Adapter:
            async def search(self, _query, limit=5):
                return [
                    main.SearchHit(
                        source_id=source.id,
                        source_name=source.name,
                        canon=source.canon,
                        title="Baby Partygoers",
                        url="https://example.fandom.com/wiki/Baby_Partygoers",
                        score=0.64,
                    )
                ]

            async def fetch_hit(self, hit, **_kwargs):
                return main.PagePayload(
                    source_id=source.id,
                    source_name=source.name,
                    canon=source.canon,
                    title=hit.title,
                    url=hit.url,
                    text="fixture",
                    headings=[],
                    links=[],
                    image_urls=[],
                    analysis={},
                    retrieved_at="2026-07-10T00:00:00+00:00",
                )

        with patch.object(main, "adapter_for", return_value=Adapter()):
            with self.assertRaises(main.PageNotFound):
                asyncio.run(
                    main.OmniLoreEngine(
                        FixtureRegistry(source)
                    ).resolve_and_fetch(
                        "Baby Food",
                        source_ids=[source.id],
                    )
                )

    def test_resolver_accepts_generic_display_title_with_exact_slug(self):
        source = self.source

        class Adapter:
            async def search(self, _query, limit=5):
                return [
                    main.SearchHit(
                        source_id=source.id,
                        source_name=source.name,
                        canon=source.canon,
                        title="Liminal Archives",
                        url="https://example.fandom.com/wiki/Baby_Food",
                        score=1.0,
                    )
                ]

            async def fetch_hit(self, hit, **_kwargs):
                return main.PagePayload(
                    source_id=source.id,
                    source_name=source.name,
                    canon=source.canon,
                    title="Baby Food",
                    url=hit.url,
                    text="fixture",
                    headings=[],
                    links=[],
                    image_urls=[],
                    analysis={},
                    retrieved_at="2026-07-10T00:00:00+00:00",
                )

        with patch.object(main, "adapter_for", return_value=Adapter()):
            result = asyncio.run(
                main.OmniLoreEngine(
                    FixtureRegistry(source)
                ).resolve_and_fetch(
                    "Baby Food",
                    source_ids=[source.id],
                )
            )
        self.assertEqual(result["page"]["title"], "Baby Food")


class GraphSignalHotfixTests(unittest.TestCase):
    def test_graph_filters_media_links_and_generic_signals(self):
        indexer = AtlasIndexer(None, None, None, None)
        doc = {
            "source_id": "freewriting-fandom",
            "url": "https://backrooms-freewriting.fandom.com/wiki/Level_8887",
        }
        page = {
            "links": [
                {
                    "title": "BusStopShelter.png",
                    "url": "https://backrooms-freewriting.fandom.com/wiki/File:BusStopShelter.png",
                },
                {
                    "title": "Level 69",
                    "url": "https://backrooms-freewriting.fandom.com/wiki/Level_69",
                },
            ],
            "analysis": {
                "named_signals": {
                    "level_designations": [
                        "Level is",
                        "Level whose",
                        "Level 69",
                        "Level Fun",
                    ],
                    "entity_designations": [
                        "Entity count",
                        "Entity 3",
                    ],
                    "groups": [],
                }
            },
        }

        edges = indexer._edges(doc, page)
        titles = {edge["to_title"] for edge in edges}

        self.assertNotIn("BusStopShelter.png", titles)
        self.assertNotIn("Level is", titles)
        self.assertNotIn("Level whose", titles)
        self.assertNotIn("Entity count", titles)
        self.assertIn("Level 69", titles)
        self.assertIn("Level Fun", titles)
        self.assertIn("Entity 3", titles)


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "query": {
                "pages": [
                    {
                        "pageid": 1,
                        "title": "File:School bus interior.jpg",
                        "imageinfo": [
                            {
                                "url": "https://upload.wikimedia.org/example.jpg",
                                "descriptionurl": "https://commons.wikimedia.org/wiki/File:School_bus_interior.jpg",
                                "mime": "image/jpeg",
                                "width": 1600,
                                "height": 1000,
                                "extmetadata": {
                                    "ImageDescription": {
                                        "value": "Interior aisle and seats of a school bus"
                                    },
                                    "Artist": {"value": "Fixture Author"},
                                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                    "LicenseUrl": {
                                        "value": "https://creativecommons.org/licenses/by-sa/4.0/"
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        }


class FakeNetwork:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(), []


class CommonsHotfixTests(unittest.TestCase):
    def test_commons_uses_identity_encoding_and_returns_image(self):
        network = FakeNetwork()
        media = MediaResearch(network)

        result = asyncio.run(
            media.search_commons("school bus interior", limit=5)
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result_count"], 1)
        self.assertTrue(network.calls)
        headers = network.calls[0][2]["extra_headers"]
        self.assertEqual(headers["Accept-Encoding"], "identity")


if __name__ == "__main__":
    unittest.main()
