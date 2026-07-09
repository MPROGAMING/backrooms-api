"""Atlas package bootstrap for BackroomsGPT v21."""

from __future__ import annotations

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


_install_resolver_identity_guard()
