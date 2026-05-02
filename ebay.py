# Reappraise was built to automate the manual price lookup process at a Habitat for
# Humanity ReStore, where staff price donated goods at roughly 60% of market value.
# This module scrapes eBay completed/sold listings to find that market value and
# applies the condition-based discount.
#
# eBay runs a JavaScript browser challenge (Akamai) that blocks plain HTTP clients
# even with correct TLS fingerprints. Playwright executes the challenge the same way
# a real Chrome browser does, so it gets through to the actual search results.

import os
import random
import re
import statistics
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Playwright, sync_playwright


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

_EBAY_SEARCH_URL = "https://www.ebay.com/sch/i.html"
_PRICE_STRIP = re.compile(r"[$,]")
_CACHE_TTL = 2 * 3600  # 2 hours in seconds


class EbayClient:
    def __init__(self) -> None:
        self._use_mock = os.getenv("EBAY_MOCK", "false").lower() == "true"
        self._last_request_at: float = 0.0
        # In-memory price cache: normalized_query → (monotonic_ts, prices)
        self._price_cache: dict[str, tuple[float, list[float]]] = {}
        # Persistent browser — initialised lazily on first scrape call
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    # -- public ----------------------------------------------------------------

    def search_prices(self, query: str, condition: str = "fair", limit: int = 20) -> PriceEstimate:
        if self._use_mock:
            return self._mock_estimate(query, condition)
        return self._scrape_estimate(query, condition, limit)

    def close(self) -> None:
        """Release the persistent Playwright browser. Call when shutting down."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # -- cache -----------------------------------------------------------------

    def _cache_key(self, query: str) -> str:
        return query.strip().lower()

    def _cache_get(self, query: str) -> list[float] | None:
        entry = self._price_cache.get(self._cache_key(query))
        if entry is None:
            return None
        ts, prices = entry
        if time.monotonic() - ts > _CACHE_TTL:
            del self._price_cache[self._cache_key(query)]
            return None
        return prices

    def _cache_set(self, query: str, prices: list[float]) -> None:
        self._price_cache[self._cache_key(query)] = (time.monotonic(), prices)

    # -- browser ---------------------------------------------------------------

    def _get_page(self):  # type: ignore[return]
        """Return a fresh page from the persistent browser, starting it if needed."""
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._playwright.chromium.launch(headless=True)
        return self._browser.new_page()

    # -- scraper ---------------------------------------------------------------

    def _throttle(self) -> None:
        """Enforce a random 1–3 s gap between consecutive live requests."""
        elapsed = time.monotonic() - self._last_request_at
        gap = random.uniform(1.0, 3.0)
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_request_at = time.monotonic()

    def _build_estimate(self, prices: list[float], condition: str, limit: int) -> PriceEstimate:
        multiplier = CONDITION_MULTIPLIERS.get(condition, CONDITION_MULTIPLIERS["fair"])
        prices = prices[:limit]
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
        )

    def _scrape_estimate(self, query: str, condition: str, limit: int) -> PriceEstimate:
        # Cache hit — reapply the condition multiplier to the stored prices
        cached = self._cache_get(query)
        if cached:
            return self._build_estimate(cached, condition, limit)

        self._throttle()
        try:
            params = {
                "_nkw": query,
                "LH_Sold": "1",     # sold listings only
                "LH_Complete": "1", # completed listings
                "_ipg": str(limit),
            }
            url = f"{_EBAY_SEARCH_URL}?{urlencode(params)}"

            page = self._get_page()
            try:
                # "load" fires after JS runs; 3 s extra lets lazy price widgets settle
                page.goto(url, wait_until="load", timeout=30_000)
                page.wait_for_timeout(3_000)
                html = page.content()
            finally:
                page.close()

            soup = BeautifulSoup(html, "html.parser")
            prices: list[float] = []
            for el in soup.select("span.s-card__price"):
                # Skip crossed-out asking prices on Best-Offer-accepted listings —
                # the actual accepted amount is not disclosed, so the number is wrong
                if "strikethrough" in (el.get("class") or []):
                    continue
                text = el.get_text(strip=True)
                # Skip price ranges like "$10.00 to $50.00"
                if " to " in text.lower():
                    continue
                cleaned = _PRICE_STRIP.sub("", text).strip()
                try:
                    prices.append(float(cleaned))
                except ValueError:
                    continue

            prices = prices[:limit]
        except Exception:
            # Any browser or network error — fall back to mock so the pipeline
            # keeps running rather than returning a 502 to the caller
            return self._mock_estimate(query, condition)

        if not prices:
            # Page loaded but no parseable prices (bot-check, zero results, layout
            # change) — fall back to mock rather than returning a dead estimate
            return self._mock_estimate(query, condition)

        self._cache_set(query, prices)
        return self._build_estimate(prices, condition, limit)

    # -- mock ------------------------------------------------------------------

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
        )
