import pytest

from ebay import EbayClient, PriceEstimate


def test_mock_mode_activates_without_credentials(monkeypatch):
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    assert EbayClient()._use_mock is True


def test_mock_mode_activates_with_placeholder_credentials(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "your-app-id-here")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "your-client-secret-here")
    assert EbayClient()._use_mock is True


def test_mock_mode_forced_by_env_var(monkeypatch):
    monkeypatch.setenv("EBAY_MOCK", "true")
    monkeypatch.setenv("EBAY_APP_ID", "some-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "some-secret")
    assert EbayClient()._use_mock is True


def test_mock_returns_valid_estimate(monkeypatch):
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
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
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    assert EbayClient().search_prices(query).low == expected_low


def test_mock_fallback_for_unknown_item(monkeypatch):
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    estimate = EbayClient().search_prices("xyzzy unknown 99999")
    assert estimate.low == 5.0
    assert estimate.market_median == 25.0
    assert estimate.high == 80.0
