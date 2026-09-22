from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app
from db import DbConfig
from ebay import PriceEstimate
from vision import ItemLabel

client = TestClient(app)

_FAKE_IMAGE = b"\xff\xd8\xff"  # minimal JPEG magic bytes
_LABELS = [ItemLabel("camera", 0.95), ItemLabel("electronics", 0.80)]
_ESTIMATE = PriceEstimate(
    low=40.0, market_median=120.0, estimated_resale_value=72.0,
    high=350.0, currency="USD", sample_size=15, is_mock=True,
    condition="fair", multiplier_used=0.6, source="mock",
)
_EMPTY_ESTIMATE = PriceEstimate(
    low=0.0, market_median=0.0, estimated_resale_value=0.0,
    high=0.0, currency="USD", sample_size=0, is_mock=False,
    condition="fair", multiplier_used=0.6, source="browse_api",
)
_FAKE_DB = DbConfig(host="h", port=1, user="u", password="p", database="d")


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """Keep unit tests off MySQL — .env may configure a real database.

    Tests that exercise persistence set app_module._db_config themselves and
    patch connect(); tests/test_db_integration.py covers the real database.
    """
    monkeypatch.setattr(app_module, "_db_config", None)


@contextmanager
def _fake_connect(_config):
    yield MagicMock()


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
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
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
        patch.object(app_module._ebay, "search_labels", return_value=_EMPTY_ESTIMATE),
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
        with patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE) as mock_search:
            _post(condition="good")

    mock_search.assert_called_once_with(["camera", "electronics"], condition="good")


def test_invalid_condition_returns_422():
    resp = _post(condition="banana")

    assert resp.status_code == 422
    assert "condition must be one of" in resp.json()["detail"]


def test_item_id_is_none_when_persistence_disabled():
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
    ):
        resp = _post()

    assert resp.status_code == 200
    assert resp.json()["item_id"] is None


def test_appraisal_is_persisted_when_database_configured(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "save_appraisal", return_value=42) as mock_save,
    ):
        resp = _post()

    assert resp.status_code == 200
    assert resp.json()["item_id"] == 42
    _, kwargs = mock_save.call_args
    assert kwargs["category"] == "camera"
    assert kwargs["description"] == "camera electronics"
    assert kwargs["estimate"] is _ESTIMATE


def test_generic_top_label_is_not_used_as_category(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    labels = [ItemLabel("gadget", 0.9), ItemLabel("game controller", 0.85)]
    with (
        patch.object(app_module._vision, "identify_item", return_value=labels),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "save_appraisal", return_value=42) as mock_save,
    ):
        resp = _post()

    assert resp.json()["item"] == "game controller"
    assert mock_save.call_args.kwargs["category"] == "game controller"
    # Description keeps Vision's raw labels as a record of what was seen
    assert mock_save.call_args.kwargs["description"] == "gadget game controller"


def test_all_generic_labels_fall_back_to_top_label_as_category(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module._vision, "identify_item",
                     return_value=[ItemLabel("gadget", 0.9), ItemLabel("technology", 0.8)]),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "save_appraisal", return_value=42) as mock_save,
    ):
        _post()

    assert mock_save.call_args.kwargs["category"] == "gadget"


def test_persistence_failure_still_returns_appraisal(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
        patch.object(app_module, "connect", _fake_connect),
        patch.object(
            app_module.repository, "save_appraisal", side_effect=RuntimeError("db down")
        ),
    ):
        resp = _post()

    assert resp.status_code == 200
    assert resp.json()["price_estimate"]["market_median"] == 120.0
    assert resp.json()["item_id"] is None
    assert "Failed to persist appraisal" in caplog.text


@pytest.mark.parametrize("path", [
    "/history",
    "/history/1",
    "/analytics/categories",
    "/analytics/price-range",
    "/analytics/top-category",
])
def test_read_endpoints_return_503_without_database(path):
    resp = client.get(path)

    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


def test_read_endpoint_returns_503_when_database_unreachable(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    @contextmanager
    def _refuse(_config):
        raise ConnectionRefusedError("no server")
        yield  # pragma: no cover

    with patch.object(app_module, "connect", _refuse):
        resp = client.get("/history")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Database unavailable"


def test_history_limit_is_validated(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    assert client.get("/history?limit=0").status_code == 422
    assert client.get("/history?limit=101").status_code == 422


def test_upstream_error_details_are_not_leaked_to_client():
    leaky = Exception("403 Forbidden for url https://vision.example/annotate?key=SECRET")
    with patch.object(app_module._vision, "identify_item", side_effect=leaky):
        resp = _post()

    assert resp.status_code == 502
    assert "SECRET" not in resp.text
