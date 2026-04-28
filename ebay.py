# Reappraise was built to automate the manual price lookup process at a Habitat for
# Humanity ReStore, where staff price donated goods at roughly 60% of current market
# value. This module fetches that market value from eBay and applies the discount.

import os
import statistics
import time
from dataclasses import dataclass

import httpx


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

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"


def _looks_real(value: str) -> bool:
    # Placeholder strings from .env.example start with "your_" or "your-"
    return bool(value) and not value.lower().startswith("your")


class EbayClient:
    def __init__(self) -> None:
        self.app_id = os.getenv("EBAY_APP_ID", "")
        self.client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
        # Auto-enable mock when credentials are absent or still hold placeholder values
        self._use_mock = (
            os.getenv("EBAY_MOCK", "false").lower() == "true"
            or not (_looks_real(self.app_id) and _looks_real(self.client_secret))
        )
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def search_prices(self, query: str, condition: str = "fair", limit: int = 20) -> PriceEstimate:
        if self._use_mock:
            return self._mock_estimate(query, condition)
        return self._live_estimate(query, condition, limit)

    # -- live ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        # 60-second buffer: avoids a race where the token is valid at check time
        # but has expired by the time the downstream API call completes
        if self._token and time.monotonic() < self._token_expiry - 60:
            return self._token
        resp = httpx.post(
            _TOKEN_URL,
            auth=(self.app_id, self.client_secret),
            data={"grant_type": "client_credentials", "scope": _EBAY_SCOPE},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.monotonic() + payload["expires_in"]
        return self._token

    def _live_estimate(self, query: str, condition: str, limit: int) -> PriceEstimate:
        resp = httpx.get(
            _SEARCH_URL,
            headers={"Authorization": f"Bearer {self._ensure_token()}"},
            params={"q": query, "limit": limit, "sort": "price"},  # ascending: cheapest first
            timeout=10,
        )
        resp.raise_for_status()
        summaries = resp.json().get("itemSummaries", [])
        prices = [
            float(item["price"]["value"])
            for item in summaries
            if "price" in item
        ]
        multiplier = CONDITION_MULTIPLIERS.get(condition, CONDITION_MULTIPLIERS["fair"])
        if not prices:
            return PriceEstimate(
                low=0.0, market_median=0.0, estimated_resale_value=0.0,
                high=0.0, currency="USD", sample_size=0, is_mock=False,
                condition=condition, multiplier_used=multiplier,
            )
        currency = summaries[0]["price"].get("currency", "USD") if summaries else "USD"
        # Median is more resistant to outliers than mean — one $5,000 listing
        # shouldn't inflate the estimate for an item that typically sells for $50
        market_median = round(statistics.median(prices), 2)
        return PriceEstimate(
            low=round(min(prices), 2),
            market_median=market_median,
            estimated_resale_value=round(market_median * multiplier, 2),
            high=round(max(prices), 2),
            currency=currency,
            sample_size=len(prices),
            is_mock=False,
            condition=condition,
            multiplier_used=multiplier,
        )

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
