# Reappraise was built to automate the manual price lookup process at a Habitat for
# Humanity ReStore, where staff price donated goods at roughly 60% of market value.
# This module looks up that market value with eBay's official Browse API and applies
# the condition-based discount. When the API can't price an item, it falls back to a
# clearly-labelled mock catalog (is_mock=True, source="mock") — never to scraping.

import base64
import logging
import os
import re
import statistics
import time
from dataclasses import dataclass

import httpx


# Values for PriceEstimate.source — persisted to price_estimates.source.
# The Browse API returns active listings (asking prices). "sold_scrape" is still
# accepted by the schema for historical rows but is no longer produced.
SOURCE_BROWSE_API = "browse_api"
SOURCE_MOCK = "mock"

logger = logging.getLogger("reappraise.ebay")

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_API_SCOPE = "https://api.ebay.com/oauth/api_scope"
_HTTP_TIMEOUT_S = 10.0

# eBay error bodies explain the failure (invalid_client, rate limit, bad query)
# and are short; cap them so an HTML error page can't flood the log
_ERROR_BODY_MAX_CHARS = 300

# A median of fewer listings than this is too noisy to trust, so the next
# Vision label is tried before settling for it
_MIN_LISTINGS = 5
# Each label costs one API call, and lower-confidence labels describe the item
# less reliably, so broadening stops after the top few usable labels
_MAX_LABELS_TO_TRY = 3

# Vision often ranks a catch-all label first ("Gadget" for a game controller).
# Searched alone, such a term prices the item against every cheap gadget on eBay,
# so any label containing one of these words is never used as a search query.
_GENERIC_LABEL_WORDS = frozenset({
    "gadget", "technology", "product", "object", "item", "equipment", "supplies",
})
_WORD = re.compile(r"[a-z]+")

_CACHE_TTL = 2 * 3600  # 2 hours in seconds
_TOKEN_REFRESH_BUFFER_S = 60


def _describe_error(exc: Exception) -> str:
    """One-line failure description, including eBay's response body for HTTP errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:_ERROR_BODY_MAX_CHARS]
        return f"HTTP {exc.response.status_code}: {body}"
    return f"{type(exc).__name__}: {exc}"


def specific_labels(labels: list[str]) -> list[str]:
    """Labels specific enough to price an item, in their original (confidence) order."""
    return [
        label for label in labels
        if _GENERIC_LABEL_WORDS.isdisjoint(_WORD.findall(label.lower()))
    ]


@dataclass(frozen=True)
class SampledListing:
    price: float
    ebay_listing_id: str | None = None


@dataclass
class PriceEstimate:
    low: float
    market_median: float
    estimated_resale_value: float  # market_median * multiplier_used
    high: float
    currency: str
    sample_size: int
    is_mock: bool
    condition: str
    multiplier_used: float
    source: str
    # The individual listings the median was computed from; empty for mock
    listings: tuple[SampledListing, ...] = ()


_MOCK_CATALOG: dict[str, tuple[float, float, float]] = {
    "camera": (40.0, 120.0, 350.0),
    "laptop": (80.0, 250.0, 600.0),
    "phone": (50.0, 180.0, 500.0),
    "book": (2.0, 8.0, 25.0),
    "toy": (5.0, 15.0, 45.0),
    "clothing": (3.0, 18.0, 60.0),
    "jacket": (10.0, 40.0, 120.0),
    "shoe": (10.0, 35.0, 120.0),
    "bag": (8.0, 30.0, 100.0),
    "watch": (20.0, 85.0, 400.0),
    "furniture": (30.0, 150.0, 500.0),
    "bicycle": (30.0, 95.0, 300.0),
    "electronics": (15.0, 75.0, 300.0),
}
_FALLBACK_MOCK = (5.0, 25.0, 80.0)

# "fair" is the ReStore baseline — matching the standard 60% pricing policy.
# Conditions above "fair" shift the price up; conditions below shift it down.
CONDITION_MULTIPLIERS: dict[str, float] = {
    "terrible": 0.4,
    "poor": 0.5,
    "fair": 0.6,
    "good": 0.7,
    "like_new": 0.8,
}


class EbayClient:
    def __init__(self) -> None:
        self._use_mock = os.getenv("EBAY_MOCK", "false").lower() == "true"
        # In-memory price cache: normalized_query → (monotonic_ts, listings)
        self._price_cache: dict[str, tuple[float, tuple[SampledListing, ...]]] = {}
        # Browse API OAuth token cache
        self._api_token: str | None = None
        self._api_token_expiry: float = 0.0

    # -- public ----------------------------------------------------------------

    def search_labels(
        self, labels: list[str], condition: str = "fair", limit: int = 20
    ) -> PriceEstimate:
        """Price an item from its Vision labels, ranked most-confident first.

        Each label is searched on its own — joining them into one query AND's the
        terms together and matches almost nothing. Generic labels ("gadget") are
        skipped. The top remaining label is tried first; if it has fewer than
        _MIN_LISTINGS priced listings, the next labels are tried one at a time and
        the largest sample wins. Mock pricing is used only when no label is usable,
        none finds anything, or the API itself fails.
        """
        mock_query = " ".join(labels)
        if self._use_mock:
            return self._mock_estimate(mock_query, condition)
        if not self._has_api_credentials():
            logger.warning("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set — cannot query eBay")
            return self._fallback_to_mock(mock_query, condition)

        searchable = specific_labels(labels)
        if not searchable:
            logger.warning("All Vision labels are too generic to search: %r", labels)
            return self._fallback_to_mock(mock_query, condition)
        if searchable[0] != labels[0]:
            logger.warning("Skipping generic label(s) %r", labels[:labels.index(searchable[0])])

        best: tuple[SampledListing, ...] = ()
        for label in searchable[:_MAX_LABELS_TO_TRY]:
            try:
                listings = self._search_listings(label, limit)
            except Exception as exc:
                # Auth, rate-limit and network failures won't be fixed by another label
                logger.warning("eBay Browse API failed for %r — %s", label, _describe_error(exc))
                return self._fallback_to_mock(mock_query, condition)
            if len(listings) >= _MIN_LISTINGS:
                return self._build_estimate(listings, condition, limit)
            logger.warning(
                "eBay Browse API found %d priced listing(s) for %r (need %d) — trying next label",
                len(listings), label, _MIN_LISTINGS,
            )
            if len(listings) > len(best):
                best = listings

        if best:
            return self._build_estimate(best, condition, limit)
        return self._fallback_to_mock(mock_query, condition)

    def search_prices(self, query: str, condition: str = "fair", limit: int = 20) -> PriceEstimate:
        """Price a single search query (no label broadening)."""
        return self.search_labels([query], condition=condition, limit=limit)

    # -- browse api ------------------------------------------------------------

    def _has_api_credentials(self) -> bool:
        return bool(os.getenv("EBAY_CLIENT_ID")) and bool(os.getenv("EBAY_CLIENT_SECRET"))

    def _fetch_api_token(self) -> str:
        """Return a cached OAuth token, requesting a new one when expired. Raises on failure."""
        if self._api_token and time.monotonic() < self._api_token_expiry:
            return self._api_token
        client_id = os.getenv("EBAY_CLIENT_ID", "")
        client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = httpx.post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": _API_SCOPE},
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._api_token = payload["access_token"]
        # Refresh slightly early so the token can't expire mid-request
        self._api_token_expiry = (
            time.monotonic() + payload.get("expires_in", 7200) - _TOKEN_REFRESH_BUFFER_S
        )
        return self._api_token

    def _search_listings(self, query: str, limit: int) -> tuple[SampledListing, ...]:
        """Priced listings for one query (possibly empty). Raises on API failure."""
        cached = self._cache_get(query)
        if cached is not None:
            return cached
        resp = httpx.get(
            _SEARCH_URL,
            params={"q": query, "limit": limit},
            headers={"Authorization": f"Bearer {self._fetch_api_token()}"},
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        listings: list[SampledListing] = []
        for item in resp.json().get("itemSummaries", []):
            try:
                price = float(item["price"]["value"])
            except (KeyError, ValueError, TypeError):
                continue
            listing_id = item.get("legacyItemId") or item.get("itemId")
            listings.append(SampledListing(price, str(listing_id) if listing_id else None))
        result = tuple(listings)
        if result:
            self._cache_set(query, result)
        return result

    def _build_estimate(
        self, listings: tuple[SampledListing, ...], condition: str, limit: int
    ) -> PriceEstimate:
        multiplier = CONDITION_MULTIPLIERS.get(condition, CONDITION_MULTIPLIERS["fair"])
        listings = listings[:limit]
        prices = [listing.price for listing in listings]
        # Median is more resistant to outliers than mean — one $5,000 listing
        # shouldn't inflate the estimate for an item that typically sells for $50
        market_median = round(statistics.median(prices), 2)
        return PriceEstimate(
            low=round(min(prices), 2),
            market_median=market_median,
            estimated_resale_value=round(market_median * multiplier, 2),
            high=round(max(prices), 2),
            currency="USD",
            sample_size=len(prices),
            is_mock=False,
            condition=condition,
            multiplier_used=multiplier,
            source=SOURCE_BROWSE_API,
            listings=listings,
        )

    # -- cache -----------------------------------------------------------------

    def _cache_key(self, query: str) -> str:
        return query.strip().lower()

    def _cache_get(self, query: str) -> tuple[SampledListing, ...] | None:
        entry = self._price_cache.get(self._cache_key(query))
        if entry is None:
            return None
        ts, listings = entry
        if time.monotonic() - ts > _CACHE_TTL:
            del self._price_cache[self._cache_key(query)]
            return None
        return listings

    def _cache_set(self, query: str, listings: tuple[SampledListing, ...]) -> None:
        self._price_cache[self._cache_key(query)] = (time.monotonic(), listings)

    # -- mock ------------------------------------------------------------------

    def _fallback_to_mock(self, query: str, condition: str) -> PriceEstimate:
        """Mock estimate after live pricing failed; the causes are logged just before."""
        logger.warning("eBay could not price %r — falling back to mock pricing", query)
        return self._mock_estimate(query, condition)

    def _mock_estimate(self, query: str, condition: str) -> PriceEstimate:
        q = query.lower()
        low, mid, high = next(
            (v for k, v in _MOCK_CATALOG.items() if k in q),
            _FALLBACK_MOCK,
        )
        multiplier = CONDITION_MULTIPLIERS.get(condition, CONDITION_MULTIPLIERS["fair"])
        return PriceEstimate(
            low=low,
            market_median=mid,
            estimated_resale_value=round(mid * multiplier, 2),
            high=high,
            currency="USD",
            sample_size=20,
            is_mock=True,
            condition=condition,
            multiplier_used=multiplier,
            source=SOURCE_MOCK,
        )
