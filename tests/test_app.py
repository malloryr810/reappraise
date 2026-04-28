from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app
from ebay import PriceEstimate
from vision import ItemLabel

client = TestClient(app)

_FAKE_IMAGE = b"\xff\xd8\xff"  # minimal JPEG magic bytes
_LABELS = [ItemLabel("camera", 0.95), ItemLabel("electronics", 0.80)]
_ESTIMATE = PriceEstimate(
    low=40.0, market_median=120.0, estimated_resale_value=72.0,
    high=350.0, currency="USD", sample_size=15, is_mock=True,
    condition="fair", multiplier_used=0.6,
)
_EMPTY_ESTIMATE = PriceEstimate(
    low=0.0, market_median=0.0, estimated_resale_value=0.0,
    high=0.0, currency="USD", sample_size=0, is_mock=False,
    condition="fair", multiplier_used=0.6,
)


def _post(image_bytes=_FAKE_IMAGE, content_type="image/jpeg", condition=None):
    params = {"condition": condition} if condition is not None else {}
    return client.post(
        "/appraise",
        files={"file": ("test.jpg", image_bytes, content_type)},
        params=params,
    )


def test_happy_path_returns_full_response():
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_prices", return_value=_ESTIMATE),
    ):
        resp = _post()

    assert resp.status_code == 200
    body = resp.json()
    assert body["item"] == "camera"
    assert len(body["labels"]) == 2
    assert body["price_estimate"]["low"] == 40.0
    assert body["price_estimate"]["market_median"] == 120.0
    assert body["price_estimate"]["estimated_resale_value"] == 72.0
    assert body["price_estimate"]["condition"] == "fair"
    assert body["price_estimate"]["multiplier_used"] == 0.6
    assert body["price_estimate"]["is_mock"] is True


def test_vision_failure_returns_502():
    with patch.object(app_module._vision, "identify_item", side_effect=Exception("timeout")):
        resp = _post()

    assert resp.status_code == 502
    assert "Vision" in resp.json()["detail"]


def test_no_labels_detected_returns_422():
    with patch.object(app_module._vision, "identify_item", return_value=[]):
        resp = _post()

    assert resp.status_code == 422
    assert "No items detected" in resp.json()["detail"]


def test_no_price_data_returns_422():
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_prices", return_value=_EMPTY_ESTIMATE),
    ):
        resp = _post()

    assert resp.status_code == 422
    assert "No eBay listings" in resp.json()["detail"]


def test_non_image_file_returns_400():
    resp = _post(image_bytes=b"name,price\na,1", content_type="text/csv")

    assert resp.status_code == 400
    assert "image" in resp.json()["detail"].lower()


def test_empty_file_returns_400():
    resp = _post(image_bytes=b"")

    assert resp.status_code == 400
    assert "Empty file" in resp.json()["detail"]


def test_missing_file_returns_422():
    resp = client.post("/appraise")

    assert resp.status_code == 422


def test_condition_param_is_forwarded_to_search():
    with patch.object(app_module._vision, "identify_item", return_value=_LABELS):
        with patch.object(app_module._ebay, "search_prices", return_value=_ESTIMATE) as mock_search:
            _post(condition="good")

    mock_search.assert_called_once_with("camera electronics", condition="good")
