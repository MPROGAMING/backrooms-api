from __future__ import annotations

from dataclasses import asdict
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


class DiscoveryRejected(ValueError):
    """A seed response is outside Source Discovery's deliberately narrow scope."""


class SourceDiscovery:
    """Discover candidate public wiki hosts from a safe, allowlisted seed page.

    The scanner never fetches arbitrary URLs.  Its validator is supplied by the
    gateway so initial targets and every redirect are restricted to public
    Fandom/Wikidot platform domains and DNS-preflighted before the request.
    """

    MAX_LINKS = 100
    MAX_REDIRECTS = 3
    MAX_RESPONSE_BYTES = 1_500_000

    def __init__(self, store, network, validate_url: Callable[[str], Awaitable[str]]):
        self.store = store
        self.network = network
        self.validate_url = validate_url

    @staticmethod
    def classify(host: str):
        normalized = host.casefold().rstrip(".")
        if normalized.endswith(".fandom.com"):
            return "mediawiki-fandom"
        if normalized.endswith(".wikidot.com"):
            return "wikidot"
        return None

    async def _fetch_seed_html(self, url: str):
        current = await self.validate_url(url)
        redirects = 0
        diagnostics = []

        while True:
            response = await self.network.get_limited(
                current,
                max_bytes=self.MAX_RESPONSE_BYTES,
                timeout_seconds=8.0,
            )
            diagnostics.extend(asdict(item) for item in response.diagnostics)

            if 300 <= response.status_code < 400:
                if redirects >= self.MAX_REDIRECTS:
                    raise DiscoveryRejected("Source Discovery redirect limit exceeded.")
                location = response.headers.get("location")
                if not location:
                    raise DiscoveryRejected("Source Discovery redirect had no location.")
                current = await self.validate_url(urljoin(current, location))
                redirects += 1
                continue

            if not 200 <= response.status_code < 300:
                raise DiscoveryRejected(
                    f"Source Discovery seed returned HTTP {response.status_code}."
                )

            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise DiscoveryRejected("Source Discovery only accepts HTML seed pages.")
            return response, current, diagnostics

    async def scan(self, url: str, max_links: int = 100):
        max_links = max(1, min(int(max_links), self.MAX_LINKS))
        response, safe_url, diagnostics = await self._fetch_seed_html(url)
        host = urlparse(safe_url).hostname or ""
        platform = self.classify(host)
        if not platform:
            raise DiscoveryRejected("Source Discovery seed is not a supported wiki platform.")

        soup = BeautifulSoup(response.text, "html.parser")
        found = {}
        for anchor in soup.find_all("a", href=True)[: max_links * 4]:
            full = urljoin(safe_url, anchor["href"])
            candidate_host = (urlparse(full).hostname or "").casefold().rstrip(".")
            candidate_platform = self.classify(candidate_host)
            if not candidate_platform:
                continue
            score = 0.5
            label = (anchor.get_text(" ", strip=True) or "").casefold()
            if "backroom" in candidate_host or "backroom" in label:
                score += 0.35
            if "wiki" in label or "archive" in label:
                score += 0.1
            found[candidate_host] = max(found.get(candidate_host, 0), score)

        candidates = []
        for candidate_host, score in sorted(found.items(), key=lambda item: item[1], reverse=True):
            row = self.store.upsert_candidate(
                candidate_host,
                self.classify(candidate_host),
                safe_url,
                score,
                {"anchor_discovery": True},
            )
            candidates.append(row)

        return {
            "ok": True,
            "seed_url": safe_url,
            "seed_platform": platform,
            "candidate_count": len(candidates),
            "candidates": candidates[:max_links],
            "max_links": max_links,
            "diagnostics": diagnostics[-12:],
            "security_boundary": (
                "Seed pages and redirects are limited to public Fandom/Wikidot platform "
                "domains, DNS-preflighted, manually redirected, HTML-only, and size-bounded."
            ),
        }

    async def probe_candidate(self, candidate: dict):
        """Perform a bounded, non-promoting reachability check for one candidate.

        A successful probe only means that an allowlisted public platform page
        responded with HTML.  It never changes the candidate's approval status
        or inserts it into the source registry.
        """
        host = str(candidate.get("host") or "").casefold().rstrip(".")
        platform = self.classify(host)
        if not host or not platform:
            raise DiscoveryRejected("Candidate host is not a supported wiki platform.")
        response, safe_url, diagnostics = await self._fetch_seed_html(f"https://{host}/")
        return {
            "ok": True,
            "candidate_id": candidate.get("candidate_id"),
            "host": host,
            "platform": platform,
            "status": candidate.get("status"),
            "reachable": True,
            "url": safe_url,
            "http_status": response.status_code,
            "diagnostics": diagnostics[-12:],
            "note": "Reachable does not mean trusted, approved, or added to the source registry.",
        }
