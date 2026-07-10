"""Atlas package bootstrap for BackroomsGPT v21."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

__version__ = "21.0.0"


def _install_resolver_identity_guard() -> None:
    """Install a final canonical-identity guard on Omni resolution.

    The main gateway imports ``atlas.routes`` only after OmniLoreEngine and its
    identity helpers exist, so package initialization can safely wrap the
    resolver without modifying the large gateway module.
    """
    import main

    resolver_cls = getattr(main, "OmniLoreEngine", None)
    if resolver_cls is None:
        return

    original = resolver_cls.resolve_and_fetch
    if getattr(original, "_backroomsgpt_identity_guard", False):
        return

    async def guarded_resolve_and_fetch(self, query, *args, **kwargs):
        result = await original(self, query, *args, **kwargs)
        hit = result.get("selected_result") or {}
        if not hit:
            raise main.PageNotFound(
                f"No safely resolvable lore page matching '{query}' across selected sources."
            )

        title = str(hit.get("title") or "")
        url = str(hit.get("url") or "")
        archived = bool(hit.get("archived"))

        acceptable = (
            main.archive_candidate_is_acceptable(query, title, url)
            if archived
            else main.live_candidate_is_acceptable(query, title, url)
        )
        if not acceptable:
            raise main.PageNotFound(
                f"Resolver rejected a fuzzy candidate that did not match canonical identity for '{query}'."
            )
        return result

    guarded_resolve_and_fetch._backroomsgpt_identity_guard = True
    resolver_cls.resolve_and_fetch = guarded_resolve_and_fetch


def _install_wikidot_full_url_normalizer() -> None:
    """Accept full same-source Wikidot URLs without weakening source boundaries.

    ``/source/page`` historically accepted titles and slugs. A full URL was fed
    into the slug matrix as ordinary text, producing malformed candidate URLs and
    a false PageNotFound even when the live page existed. Normalize only URLs
    whose hostname exactly matches one of the registered source base hosts, then
    continue through the existing Wikidot retrieval and identity checks.
    """
    import main

    adapter_cls = getattr(main, "WikidotAdapter", None)
    if adapter_cls is None:
        return

    original = adapter_cls._live_fetch
    if getattr(original, "_backroomsgpt_full_url_guard", False):
        return

    def normalize_locator(adapter, page: str) -> tuple[str, str | None]:
        raw = main.normalize_query(page)
        parsed = urlparse(raw)

        if not parsed.scheme and not parsed.netloc:
            return raw, None

        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise main.BadSourceQuery(
                "Wikidot page URL must be an absolute HTTP(S) URL."
            )

        requested_host = parsed.hostname.casefold().rstrip(".")
        allowed_hosts = {
            (urlparse(base).hostname or "").casefold().rstrip(".")
            for base in adapter.source.base_urls
            if urlparse(base).hostname
        }
        if requested_host not in allowed_hosts:
            raise main.BadSourceQuery(
                "Full Wikidot page URL host does not belong to the requested source."
            )

        locator = unquote(parsed.path or "").strip("/")
        if not locator:
            raise main.BadSourceQuery(
                "Full Wikidot page URL must include a page path."
            )

        return locator, raw

    async def guarded_live_fetch(self, page: str, *, max_chars: int):
        locator, original_url = normalize_locator(self, page)
        payload = await original(self, locator, max_chars=max_chars)
        if original_url is not None:
            payload.requested_query = original_url
            payload.resolved_via = "wikidot-full-url-normalized"
        return payload

    guarded_live_fetch._backroomsgpt_full_url_guard = True
    adapter_cls._live_fetch = guarded_live_fetch


_install_resolver_identity_guard()
_install_wikidot_full_url_normalizer()
