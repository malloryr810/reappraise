import logging
from unittest.mock import MagicMock, patch

import httpx
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
# Browse API helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def api_env(monkeypatch):
    """Live mode with (fake) Browse API credentials."""
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")


def _token_resp() -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"access_token": "tok", "expires_in": 7200}
    return resp


def _search_resp(*prices: float) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "total": len(prices),
        "itemSummaries": [
            {"legacyItemId": str(1000 + i), "price": {"value": str(p)}}
            for i, p in enumerate(prices)
        ],
    }
    return resp


def _api(results_by_query: dict[str, MagicMock]):
    """Patch the token call and route each search to the response for its q param."""
    def fake_get(url, params, **_):
        return results_by_query[params["q"]]
    get = MagicMock(side_effect=fake_get)
    return patch("ebay.httpx.post", return_value=_token_resp()), patch("ebay.httpx.get", get), get


def _queries(get: MagicMock) -> list[str]:
    return [c.kwargs["params"]["q"] for c in get.call_args_list]


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.ebay.com/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, text=body, request=request)
    )


FIVE = (10.0, 20.0, 30.0, 40.0, 50.0)


# ---------------------------------------------------------------------------
# Browse API estimates
# ---------------------------------------------------------------------------

def test_browse_api_returns_real_estimate(api_env):
    post, get, _ = _api({"lamp": _search_resp(*FIVE)})
    with post, get:
        estimate = EbayClient().search_prices("lamp", condition="good")

    assert estimate.source == ebay.SOURCE_BROWSE_API
    assert estimate.is_mock is False
    assert estimate.market_median == 30.0
    assert estimate.estimated_resale_value == 21.0
    assert estimate.sample_size == 5


def test_browse_api_captures_listing_ids(api_env):
    search_resp = MagicMock()
    search_resp.json.return_value = {"itemSummaries": [
        {"legacyItemId": "110001", "itemId": "v1|110001|0", "price": {"value": "12.50"}},
        {"itemId": "v1|110002|0", "price": {"value": "30.00"}},
        {"legacyItemId": "110003", "price": {"value": "not-a-number"}},
        {"price": {"value": "20.00"}},
    ]}

    with (
        patch("ebay.httpx.post", return_value=_token_resp()),
        patch("ebay.httpx.get", return_value=search_resp),
    ):
        estimate = EbayClient().search_prices("drill")

    assert estimate.source == ebay.SOURCE_BROWSE_API
    assert estimate.is_mock is False
    assert [(l.price, l.ebay_listing_id) for l in estimate.listings] == [
        (12.5, "110001"), (30.0, "v1|110002|0"), (20.0, None),
    ]
    assert estimate.market_median == 20.0


def test_missing_credentials_falls_back_to_mock_without_http(monkeypatch, caplog):
    monkeypatch.delenv("EBAY_MOCK", raising=False)
    with patch("ebay.httpx.get") as get, patch("ebay.httpx.post") as post:
        estimate = EbayClient().search_prices("lamp")

    assert estimate.is_mock is True
    get.assert_not_called()
    post.assert_not_called()
    assert "EBAY_CLIENT_ID" in caplog.text


def test_scraper_is_gone():
    assert not hasattr(ebay, "sync_playwright")
    assert not hasattr(EbayClient, "_scrape_estimate")


# ---------------------------------------------------------------------------
# Label broadening — top label alone first, then other labels one at a time
# ---------------------------------------------------------------------------

def test_top_label_searched_alone_when_it_has_enough_results(api_env):
    post, get, get_mock = _api({"bicycle": _search_resp(*FIVE)})
    with post, get:
        estimate = EbayClient().search_labels(["bicycle", "bicycle tire", "vehicle"])

    assert _queries(get_mock) == ["bicycle"]
    assert estimate.source == ebay.SOURCE_BROWSE_API
    assert estimate.sample_size == 5


def test_broadens_to_next_label_when_top_label_has_too_few(api_env, caplog):
    post, get, get_mock = _api({
        "joystick": _search_resp(15.0),
        "game controller": _search_resp(*FIVE),
    })
    with post, get:
        estimate = EbayClient().search_labels(["joystick", "game controller", "gamepad"])

    # Each label is its own query — never one AND'd string of all labels
    assert _queries(get_mock) == ["joystick", "game controller"]
    assert estimate.market_median == 30.0
    assert estimate.is_mock is False
    assert "'joystick'" in caplog.text and "1 priced listing" in caplog.text


def test_uses_largest_real_sample_when_no_label_reaches_threshold(api_env):
    post, get, get_mock = _api({
        "a": _search_resp(10.0),
        "b": _search_resp(10.0, 20.0, 30.0),
        "c": _search_resp(),
    })
    with post, get:
        estimate = EbayClient().search_labels(["a", "b", "c"])

    assert _queries(get_mock) == ["a", "b", "c"]
    assert estimate.is_mock is False
    assert estimate.sample_size == 3
    assert estimate.market_median == 20.0


def test_tries_at_most_max_labels(api_env):
    labels = [f"label{i}" for i in range(ebay._MAX_LABELS_TO_TRY + 2)]
    post, get, get_mock = _api({label: _search_resp() for label in labels})
    with post, get:
        EbayClient().search_labels(labels)

    assert _queries(get_mock) == labels[:ebay._MAX_LABELS_TO_TRY]


def test_denylisted_top_label_is_skipped_for_next_usable_one(api_env):
    post, get, get_mock = _api({"game controller": _search_resp(*FIVE)})
    with post, get:
        estimate = EbayClient().search_labels(["gadget", "game controller", "technology"])

    assert _queries(get_mock) == ["game controller"]
    assert estimate.is_mock is False


def test_denylist_matches_generic_words_inside_labels(api_env):
    post, get, get_mock = _api({"bicycle tire": _search_resp()})
    with post, get:
        EbayClient().search_labels(["bicycles--equipment and supplies", "bicycle tire"])

    assert _queries(get_mock) == ["bicycle tire"]


def test_denylisted_labels_do_not_use_up_the_label_budget(api_env):
    labels = ["gadget", "technology", "product"] + [f"label{i}" for i in range(5)]
    post, get, get_mock = _api({label: _search_resp() for label in labels})
    with post, get:
        EbayClient().search_labels(labels)

    assert _queries(get_mock) == labels[3:3 + ebay._MAX_LABELS_TO_TRY]


def test_all_labels_generic_falls_back_to_mock_without_searching(api_env, caplog):
    with patch("ebay.httpx.get") as get, patch("ebay.httpx.post"):
        estimate = EbayClient().search_labels(["gadget", "technology"])

    get.assert_not_called()
    assert estimate.is_mock is True
    assert "too generic" in caplog.text


@pytest.mark.parametrize("labels,expected", [
    (["gadget", "game controller"], ["game controller"]),
    (["Technology", "Electronic Device"], ["Electronic Device"]),
    (["sports equipment", "bicycle"], ["bicycle"]),
    (["gadget"], []),
])
def test_specific_labels_filters_generic_terms(labels, expected):
    assert ebay.specific_labels(labels) == expected


def test_mock_only_when_every_label_returns_nothing(api_env, caplog):
    post, get, _ = _api({"bicycle": _search_resp(), "wheel": _search_resp()})
    with post, get:
        estimate = EbayClient().search_labels(["bicycle", "wheel"])

    assert estimate.is_mock is True
    assert estimate.market_median == 95.0  # mock catalog matched "bicycle"
    assert "falling back to mock pricing" in caplog.text


def test_api_error_stops_broadening_and_falls_back_to_mock(api_env, caplog):
    search_resp = MagicMock()
    search_resp.raise_for_status.side_effect = _http_error(429, "rate limited")
    with (
        patch("ebay.httpx.post", return_value=_token_resp()),
        patch("ebay.httpx.get", return_value=search_resp) as get,
    ):
        estimate = EbayClient().search_labels(["lamp", "light", "fixture"])

    # A rate limit or auth failure won't be fixed by a different label
    assert get.call_count == 1
    assert estimate.is_mock is True
    assert "429" in caplog.text and "rate limited" in caplog.text


def test_token_failure_is_logged_with_status_and_body(api_env, caplog):
    token_resp = MagicMock()
    token_resp.raise_for_status.side_effect = _http_error(401, '{"error":"invalid_client"}')
    with (
        patch("ebay.httpx.post", return_value=token_resp),
        patch("ebay.httpx.get") as get,
    ):
        estimate = EbayClient().search_labels(["lamp", "light"])

    get.assert_not_called()
    assert estimate.is_mock is True
    assert "401" in caplog.text and "invalid_client" in caplog.text


def test_mock_fallback_is_a_warning(api_env, caplog):
    post, get, _ = _api({"lamp": _search_resp()})
    with caplog.at_level(logging.WARNING, logger="reappraise.ebay"), post, get:
        EbayClient().search_prices("lamp")

    fallback = [r for r in caplog.records if "mock pricing" in r.getMessage()]
    assert fallback and fallback[0].levelno == logging.WARNING


def test_forced_mock_mode_does_not_log_fallback(monkeypatch, caplog):
    monkeypatch.setenv("EBAY_MOCK", "true")
    EbayClient().search_labels(["lamp"])

    assert "mock pricing" not in caplog.text


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

def test_cache_hit_skips_second_api_call(api_env):
    post, get, get_mock = _api({"lamp": _search_resp(*FIVE)})
    client = EbayClient()
    with post, get:
        first = client.search_prices("lamp")
        second = client.search_prices("lamp", condition="good")

    assert get_mock.call_count == 1
    assert second.listings == first.listings
    assert second.source == ebay.SOURCE_BROWSE_API
    # Cached prices still get the new condition's multiplier
    assert (first.multiplier_used, second.multiplier_used) == (0.6, 0.7)
    assert second.estimated_resale_value > first.estimated_resale_value


def test_cache_expires_after_ttl(api_env):
    post, get, get_mock = _api({"lamp": _search_resp(*FIVE)})
    client = EbayClient()
    with post, get:
        client.search_prices("lamp")
        # Wind the cache entry's timestamp back past the TTL
        key = client._cache_key("lamp")
        ts, sample = client._price_cache[key]
        client._price_cache[key] = (ts - ebay._CACHE_TTL - 1, sample)
        client.search_prices("lamp")

    assert get_mock.call_count == 2


def test_cache_key_normalises_query(api_env):
    post, get, get_mock = _api({"  Canon Camera  ": _search_resp(*FIVE)})
    client = EbayClient()
    with post, get:
        client.search_prices("  Canon Camera  ")
        client.search_prices("canon camera")

    assert get_mock.call_count == 1


def test_mock_estimate_has_no_listings_and_mock_source(monkeypatch):
    monkeypatch.setenv("EBAY_MOCK", "true")
    estimate = EbayClient().search_prices("camera")
    assert estimate.source == ebay.SOURCE_MOCK
    assert estimate.listings == ()
