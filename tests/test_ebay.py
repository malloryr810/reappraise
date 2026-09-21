import time
from unittest.mock import MagicMock, patch

import pytest

import ebay
from ebay import EbayClient, PriceEstimate


# ---------------------------------------------------------------------------
# Mock-mode activation
# ---------------------------------------------------------------------------

def test_mock_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    assert EbayClient()._use_mock is False


def test_mock_mode_forced_by_env_var(monkeypatch):
    monkeypatch.setenv("EBAY_MOCK", "true")
    assert EbayClient()._use_mock is True


# ---------------------------------------------------------------------------
# Mock-path estimates
# ---------------------------------------------------------------------------

def test_mock_returns_valid_estimate(monkeypatch):
    monkeypatch.setenv("EBAY_MOCK", "true")
    estimate = EbayClient().search_prices("vintage camera")
    assert isinstance(estimate, PriceEstimate)
    assert estimate.is_mock is True
    assert estimate.low < estimate.market_median <= estimate.high
    assert estimate.condition == "fair"
    assert estimate.multiplier_used == 0.6
    assert estimate.estimated_resale_value == round(estimate.market_median * 0.6, 2)
    assert estimate.currency == "USD"
    assert estimate.sample_size == 20


@pytest.mark.parametrize("query,expected_low", [
    ("camera", 40.0),
    ("laptop computer", 80.0),
    ("old book", 2.0),
    ("wristwatch", 20.0),
    ("bicycle frame", 30.0),
])
def test_mock_catalog_matches_known_categories(monkeypatch, query, expected_low):
    monkeypatch.setenv("EBAY_MOCK", "true")
    assert EbayClient().search_prices(query).low == expected_low


def test_mock_fallback_for_unknown_item(monkeypatch):
    monkeypatch.setenv("EBAY_MOCK", "true")
    estimate = EbayClient().search_prices("xyzzy unknown 99999")
    assert estimate.low == 5.0
    assert estimate.market_median == 25.0
    assert estimate.high == 80.0


# ---------------------------------------------------------------------------
# Scraper-path helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Suppress the throttle delay in all tests."""
    monkeypatch.setattr("ebay.time.sleep", lambda _: None)


def _make_playwright_mock(html: str) -> MagicMock:
    """
    Return a mock for sync_playwright that uses the .start() pattern
    (not a context manager) matching the persistent-browser implementation.

    Usage:
        with patch("ebay.sync_playwright", _make_playwright_mock(html)):
            ...
    """
    page = MagicMock()
    page.content.return_value = html
    page.goto = MagicMock()
    page.wait_for_timeout = MagicMock()
    page.close = MagicMock()

    browser = MagicMock()
    browser.new_page.return_value = page
    browser.is_connected.return_value = True
    browser.close = MagicMock()

    pw_instance = MagicMock()
    pw_instance.chromium.launch.return_value = browser

    # sync_playwright() → starter; starter.start() → pw_instance
    starter = MagicMock()
    starter.start.return_value = pw_instance

    sync_pw = MagicMock(return_value=starter)
    return sync_pw


def _price_html(*prices: str) -> str:
    items = "".join(
        f'<li><span class="s-card__price">{p}</span></li>'
        for p in prices
    )
    return f"<html><body><ul>{items}</ul></body></html>"


def _browser_from_mock(mock_sync_pw: MagicMock) -> MagicMock:
    """Navigate the mock chain to the browser object."""
    return mock_sync_pw.return_value.start.return_value.chromium.launch.return_value


# ---------------------------------------------------------------------------
# Scraper-path estimates
# ---------------------------------------------------------------------------

def test_scraper_returns_estimate_from_html(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$10.00", "$20.00", "$30.00")
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("lamp")

    assert estimate.is_mock is False
    assert estimate.low == 10.0
    assert estimate.market_median == 20.0
    assert estimate.high == 30.0
    assert estimate.sample_size == 3
    assert estimate.currency == "USD"


def test_scraper_applies_condition_multiplier(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$100.00", "$200.00", "$300.00")
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("drill", condition="good")

    assert estimate.condition == "good"
    assert estimate.multiplier_used == 0.7
    assert estimate.estimated_resale_value == round(200.0 * 0.7, 2)


def test_scraper_skips_price_ranges(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$5.00 to $15.00", "$10.00")
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("mug")

    assert estimate.sample_size == 1
    assert estimate.low == estimate.high == 10.0


def test_scraper_skips_strikethrough_prices(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = (
        '<html><body>'
        '<span class="s-card__price strikethrough">$999.99</span>'
        '<span class="s-card__price">$50.00</span>'
        '</body></html>'
    )
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("camera")

    assert estimate.sample_size == 1
    assert estimate.low == 50.0


def test_scraper_falls_back_to_mock_when_no_prices(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = "<html><body><p>No results</p></body></html>"
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("xyzzy unknown item")

    assert estimate.is_mock is True
    assert estimate.sample_size == 20


def test_scraper_falls_back_to_mock_on_browser_error(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    with patch("ebay.sync_playwright", side_effect=Exception("browser crashed")):
        estimate = EbayClient().search_prices("lamp")

    assert estimate.is_mock is True


def test_scraper_strips_commas_from_prices(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$1,200.00", "$1,500.00")
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("vintage guitar")

    assert estimate.low == 1200.0
    assert estimate.high == 1500.0


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

def test_cache_hit_skips_second_scrape(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$10.00", "$20.00", "$30.00")
    mock_sp = _make_playwright_mock(html)
    client = EbayClient()

    with patch("ebay.sync_playwright", mock_sp):
        est1 = client.search_prices("lamp")
        est2 = client.search_prices("lamp")

    # Browser was only asked for a page once — second call served from cache
    assert _browser_from_mock(mock_sp).new_page.call_count == 1
    assert est1.market_median == est2.market_median


def test_cache_reapplies_multiplier_per_condition(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$100.00", "$200.00", "$300.00")
    mock_sp = _make_playwright_mock(html)
    client = EbayClient()

    with patch("ebay.sync_playwright", mock_sp):
        fair = client.search_prices("drill", condition="fair")
        good = client.search_prices("drill", condition="good")

    # Second call still hits cache but applies the new multiplier
    assert _browser_from_mock(mock_sp).new_page.call_count == 1
    assert fair.multiplier_used == 0.6
    assert good.multiplier_used == 0.7
    assert good.estimated_resale_value > fair.estimated_resale_value


def test_cache_expires_after_ttl(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$10.00", "$20.00", "$30.00")
    mock_sp = _make_playwright_mock(html)
    client = EbayClient()

    with patch("ebay.sync_playwright", mock_sp):
        client.search_prices("lamp")
        # Wind the cache entry's timestamp back past the TTL
        key = client._cache_key("lamp")
        ts, prices = client._price_cache[key]
        client._price_cache[key] = (ts - ebay._CACHE_TTL - 1, prices)
        client.search_prices("lamp")

    # Expired cache forced a second scrape
    assert _browser_from_mock(mock_sp).new_page.call_count == 2


def test_cache_key_normalises_query(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$50.00")
    mock_sp = _make_playwright_mock(html)
    client = EbayClient()

    with patch("ebay.sync_playwright", mock_sp):
        client.search_prices("  Canon Camera  ")
        client.search_prices("canon camera")

    assert _browser_from_mock(mock_sp).new_page.call_count == 1


# ---------------------------------------------------------------------------
# Persistent browser reuse
# ---------------------------------------------------------------------------

def test_browser_launched_once_across_multiple_searches(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html1 = _price_html("$10.00", "$20.00")
    html2 = _price_html("$30.00", "$40.00")

    page1, page2 = MagicMock(), MagicMock()
    page1.content.return_value = html1
    page1.goto = page1.wait_for_timeout = page1.close = MagicMock()
    page2.content.return_value = html2
    page2.goto = page2.wait_for_timeout = page2.close = MagicMock()

    browser = MagicMock()
    browser.new_page.side_effect = [page1, page2]
    browser.is_connected.return_value = True

    pw_instance = MagicMock()
    pw_instance.chromium.launch.return_value = browser

    starter = MagicMock()
    starter.start.return_value = pw_instance
    mock_sp = MagicMock(return_value=starter)

    client = EbayClient()
    with patch("ebay.sync_playwright", mock_sp):
        client.search_prices("lamp")
        client.search_prices("bicycle")

    # Playwright started once, browser launched once, two separate pages opened
    assert mock_sp.call_count == 1
    assert pw_instance.chromium.launch.call_count == 1
    assert browser.new_page.call_count == 2


def test_browser_relaunches_after_disconnect(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$50.00")

    page = MagicMock()
    page.content.return_value = html
    page.goto = page.wait_for_timeout = page.close = MagicMock()

    browser1, browser2 = MagicMock(), MagicMock()
    # browser1 reports disconnected on the second call
    browser1.is_connected.return_value = False
    browser2.new_page.return_value = page
    browser2.is_connected.return_value = True

    pw_instance = MagicMock()
    # browser1 is seeded directly (bypassing launch), so the first actual
    # chromium.launch() call (the relaunch) should return browser2
    pw_instance.chromium.launch.return_value = browser2

    starter = MagicMock()
    starter.start.return_value = pw_instance
    mock_sp = MagicMock(return_value=starter)

    client = EbayClient()
    # Seed _playwright and _browser directly — simulates a previously-live client
    # whose browser has since disconnected
    with patch("ebay.sync_playwright", mock_sp):
        client._playwright = pw_instance
        client._browser = browser1
        client.search_prices("lamp")

    # browser1 was disconnected, so _get_page() launched browser2 and used it
    assert pw_instance.chromium.launch.call_count == 1
    assert browser2.new_page.call_count == 1


# ---------------------------------------------------------------------------
# Per-listing data (persisted to listings_sampled)
# ---------------------------------------------------------------------------

def test_mock_estimate_has_no_listings_and_mock_source(monkeypatch):
    monkeypatch.setenv("EBAY_MOCK", "true")
    estimate = EbayClient().search_prices("camera")
    assert estimate.source == ebay.SOURCE_MOCK
    assert estimate.listings == ()


def test_scraper_captures_listing_ids(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = (
        "<html><body><ul>"
        '<li data-listingid="111"><span class="s-card__price">$10.00</span></li>'
        '<li><a href="https://www.ebay.com/itm/222?hash=x">link</a>'
        '<span class="s-card__price">$20.00</span></li>'
        '<li><a href="https://www.ebay.com/itm/some-title-slug/333">link</a>'
        '<span class="s-card__price">$30.00</span></li>'
        '<li><span class="s-card__price">$40.00</span></li>'
        "</ul></body></html>"
    )
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        estimate = EbayClient().search_prices("lamp")

    assert estimate.source == ebay.SOURCE_SOLD_SCRAPE
    assert [(l.price, l.ebay_listing_id) for l in estimate.listings] == [
        (10.0, "111"), (20.0, "222"), (30.0, "333"), (40.0, None),
    ]


def test_cache_hit_preserves_listings_and_source(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    html = _price_html("$10.00", "$20.00")
    client = EbayClient()
    with patch("ebay.sync_playwright", _make_playwright_mock(html)):
        first = client.search_prices("lamp")
        second = client.search_prices("lamp", condition="good")

    assert second.listings == first.listings
    assert second.source == ebay.SOURCE_SOLD_SCRAPE


def test_browse_api_captures_listing_ids(monkeypatch):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")

    token_resp = MagicMock()
    token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200}
    search_resp = MagicMock()
    search_resp.json.return_value = {"itemSummaries": [
        {"legacyItemId": "110001", "itemId": "v1|110001|0", "price": {"value": "12.50"}},
        {"itemId": "v1|110002|0", "price": {"value": "30.00"}},
        {"legacyItemId": "110003", "price": {"value": "not-a-number"}},
        {"price": {"value": "20.00"}},
    ]}

    with (
        patch("ebay.httpx.post", return_value=token_resp),
        patch("ebay.httpx.get", return_value=search_resp),
    ):
        estimate = EbayClient().search_prices("drill")

    assert estimate.source == ebay.SOURCE_BROWSE_API
    assert estimate.is_mock is False
    assert [(l.price, l.ebay_listing_id) for l in estimate.listings] == [
        (12.5, "110001"), (30.0, "v1|110002|0"), (20.0, None),
    ]
    assert estimate.market_median == 20.0
