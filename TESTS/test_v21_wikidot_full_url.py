"""Offline regression tests for Wikidot title/slug/full-URL equivalence."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

import main


def wikidot_source() -> main.SourceConfig:
    return main.SourceConfig(
        id="fixture-wikidot-full-url",
        name="Fixture Wikidot",
        kind=main.SourceKind.WIKIDOT,
        canon="Fixture",
        priority=1,
        base_urls=("https://fixture.wikidot.com",),
    )


def live_response(url: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    html = """
    <html>
      <head><title>The Backrooms - Wikidot</title></head>
      <body>
        <div id="page-content">
          <h1>The Backrooms</h1>
          <p>This fixture contains enough readable article text for the sanitizer
          and identity checks to treat the page as a live Wikidot document.</p>
        </div>
      </body>
    </html>
    """
    return httpx.Response(200, text=html, request=request)


class WikidotFullUrlRegressionTests(unittest.TestCase):
    def _fetch(self, page: str):
        source = wikidot_source()
        adapter = main.WikidotAdapter(source)

        async def fake_try(urls):
            self.assertTrue(urls)
            canonical = "https://fixture.wikidot.com/level-0"
            self.assertEqual(urls[0], canonical)
            response = live_response(canonical)
            return response, canonical, [], True

        adapter._try_url_candidates = fake_try
        return asyncio.run(
            adapter.fetch_page(
                page,
                max_chars=8000,
                allow_archive_fallback=False,
            )
        )

    def test_title_slug_and_full_url_resolve_to_same_page(self):
        title_page = self._fetch("Level 0")
        slug_page = self._fetch("level-0")
        url_page = self._fetch("https://fixture.wikidot.com/level-0")

        self.assertEqual(title_page.url, "https://fixture.wikidot.com/level-0")
        self.assertEqual(slug_page.url, title_page.url)
        self.assertEqual(url_page.url, title_page.url)
        self.assertEqual(url_page.title, "The Backrooms")
        self.assertEqual(
            url_page.resolved_via,
            "wikidot-full-url-normalized",
        )

    def test_full_url_wrong_host_is_rejected_before_network(self):
        source = wikidot_source()
        adapter = main.WikidotAdapter(source)

        async def must_not_fetch(_urls):
            raise AssertionError("mismatched host must be rejected before network")

        adapter._try_url_candidates = must_not_fetch

        with self.assertRaises(main.BadSourceQuery):
            asyncio.run(
                adapter.fetch_page(
                    "https://other.wikidot.com/level-0",
                    max_chars=8000,
                    allow_archive_fallback=False,
                )
            )

    def test_namespace_path_is_preserved(self):
        source = wikidot_source()
        adapter = main.WikidotAdapter(source)

        async def fake_try(urls):
            self.assertEqual(
                urls[0],
                "https://fixture.wikidot.com/system:recent-changes",
            )
            response = live_response(urls[0])
            return response, urls[0], [], True

        adapter._try_url_candidates = fake_try

        # The fixture article title is generic, so the normal identity checker
        # would reject it after URL construction. We only need to assert that the
        # namespace path reaches the normal URL matrix unchanged.
        with self.assertRaises(main.PageNotFound):
            asyncio.run(
                adapter.fetch_page(
                    "https://fixture.wikidot.com/system:recent-changes",
                    max_chars=8000,
                    allow_archive_fallback=False,
                )
            )


if __name__ == "__main__":
    unittest.main()
