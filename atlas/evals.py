from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unquote(value or "").casefold())


def _hit_matches_query(hit, query: str) -> bool:
    """Accept a search hit when either its title or canonical URL path matches.

    Wikidot pages may expose the site name as the HTML <h1> even when the resolved
    URL is the correct page. The old eval checked only the displayed title and could
    therefore fail while the live /baby-food page had actually been found.
    """
    target = _key(query)
    if not target:
        return False

    title_key = _key(getattr(hit, "title", ""))
    path_key = _key(urlparse(getattr(hit, "url", "")).path.strip("/"))
    return target == title_key or target == path_key


class EvalSuite:
    def __init__(self, store, omni, registry, adapter_for):
        self.store = store
        self.omni = omni
        self.registry = registry
        self.adapter_for = adapter_for

    async def run_acceptance(self, live: bool = True):
        results = []

        def add(name, passed, details):
            results.append({"name": name, "passed": bool(passed), "details": details})

        ids = set(self.registry.all_ids())
        add(
            "core_sources_registered",
            {"fandom-main", "wikidot-main", "liminal-archives"}.issubset(ids),
            sorted(ids),
        )

        if live:
            try:
                la = self.adapter_for(self.registry.get("liminal-archives"))
                hits = await la.search("Baby Food", limit=5)
                matches = [hit for hit in hits if _hit_matches_query(hit, "Baby Food")]

                details = {
                    "hits": [
                        {
                            "title": hit.title,
                            "url": hit.url,
                            "archived": bool(getattr(hit, "archived", False)),
                        }
                        for hit in hits
                    ]
                }

                if not matches:
                    add("liminal_baby_food_search", False, details)
                else:
                    best = matches[0]
                    slug = unquote(urlparse(best.url).path.strip("/")) or "Baby Food"
                    page = await la.fetch_page(
                        slug,
                        max_chars=12000,
                        allow_archive_fallback=False,
                    )
                    live_ok = (
                        not bool(getattr(page, "archived", False))
                        and _key(urlparse(page.url).path.strip("/")) == _key("Baby Food")
                    )
                    details["fetched"] = {
                        "title": page.title,
                        "url": page.url,
                        "archived": bool(page.archived),
                    }
                    add("liminal_baby_food_search", live_ok, details)
            except Exception as exc:
                add(
                    "liminal_baby_food_search",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )

            try:
                cmp = await self.omni.compare(
                    "Level 0",
                    source_ids=["wikidot-main", "fandom-main"],
                )
                ok = (
                    cmp.get("meta", {}).get("payload_mode") == "compact-comparison"
                    and len(cmp.get("records", [])) == 2
                )
                add("compact_canon_compare", ok, cmp.get("meta", {}))
            except Exception as exc:
                add("compact_canon_compare", False, f"{type(exc).__name__}: {exc}")

            try:
                la = self.adapter_for(self.registry.get("liminal-archives"))
                try:
                    page = await la.fetch_page(
                        "Level 0",
                        max_chars=10000,
                        allow_archive_fallback=True,
                    )
                    title = page.title
                    add(
                        "reject_wrong_liminal_archive_match",
                        not ("10.1" in title or "corn maze" in title.casefold()),
                        title,
                    )
                except Exception as exc:
                    add(
                        "reject_wrong_liminal_archive_match",
                        True,
                        f"correctly no unsafe match: {type(exc).__name__}",
                    )
            except Exception as exc:
                add(
                    "reject_wrong_liminal_archive_match",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )

        run = self.store.save_eval(
            "acceptance-live" if live else "acceptance-local",
            results,
        )
        return {"ok": run["failed"] == 0, **run}
