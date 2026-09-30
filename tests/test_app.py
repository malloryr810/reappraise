import logging
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pymysql
import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app
from db import DbConfig
from ebay import PriceEstimate
from labels import FALLBACK_TAXONOMY
from repository import RecordedOutcome, SavedAppraisal
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
_SAVED = SavedAppraisal(item_id=42, estimate_id=7)


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

    mock_search.assert_called_once_with(
        ["camera", "electronics"], condition="good", user_description=None,
        taxonomy=FALLBACK_TAXONOMY,
    )


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
    assert (resp.json()["item_id"], resp.json()["estimate_id"]) == (None, None)


def test_appraisal_is_persisted_when_database_configured(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE),
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "save_appraisal", return_value=_SAVED) as mock_save,
    ):
        resp = _post()

    assert resp.status_code == 200
    assert (resp.json()["item_id"], resp.json()["estimate_id"]) == (42, 7)
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
        patch.object(app_module.repository, "save_appraisal", return_value=_SAVED) as mock_save,
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
        patch.object(app_module.repository, "save_appraisal", return_value=_SAVED) as mock_save,
    ):
        _post()

    assert mock_save.call_args.kwargs["category"] == "gadget"


_SEARCHED_ESTIMATE = replace(
    _ESTIMATE, is_mock=False, source="browse_api",
    search_term="Trek Emonda SL 5", search_source="user_description",
)


def _post_description(description, estimate=_SEARCHED_ESTIMATE):
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=estimate) as mock_search,
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "save_appraisal", return_value=_SAVED) as mock_save,
    ):
        resp = client.post(
            "/appraise",
            files={"file": ("item.jpg", _FAKE_IMAGE, "image/jpeg")},
            data={"user_description": description},
        )
    return resp, mock_search, mock_save


def test_description_is_forwarded_stripped_and_persisted(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    resp, mock_search, mock_save = _post_description("  Trek Emonda SL 5  ")

    assert resp.status_code == 200
    assert mock_search.call_args.kwargs["user_description"] == "Trek Emonda SL 5"
    kwargs = mock_save.call_args.kwargs
    assert kwargs["user_description"] == "Trek Emonda SL 5"
    # Vision's output is stored untouched alongside it
    assert (kwargs["category"], kwargs["description"]) == ("camera", "camera electronics")


def test_response_reports_what_was_searched():
    resp, _, _ = _post_description("Trek Emonda SL 5")

    body = resp.json()
    assert body["user_description"] == "Trek Emonda SL 5"
    assert body["search_term"] == "Trek Emonda SL 5"
    assert body["search_source"] == "user_description"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_description_is_treated_as_absent(monkeypatch, blank):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    resp, mock_search, mock_save = _post_description(blank, estimate=_ESTIMATE)

    assert resp.status_code == 200
    assert mock_search.call_args.kwargs["user_description"] is None
    assert mock_save.call_args.kwargs["user_description"] is None
    assert resp.json()["user_description"] is None


def test_description_longer_than_column_is_rejected():
    resp, mock_search, _ = _post_description("x" * (app_module.USER_DESCRIPTION_MAX + 1))

    assert resp.status_code == 422
    mock_search.assert_not_called()


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
    assert (resp.json()["item_id"], resp.json()["estimate_id"]) == (None, None)
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


# -- outcomes and accuracy ----------------------------------------------------

_OUTCOME_ROW = {
    "outcome_id": 1, "estimate_id": 7, "final_price": 55.0, "sold_price": None,
    "sold_at": None, "created_at": "2026-09-28T10:00:00",
}


def test_accuracy_endpoint_returns_503_without_database():
    assert client.get("/analytics/accuracy").status_code == 503


def test_outcome_endpoint_returns_503_without_database():
    assert client.post("/estimates/7/outcome", json={"final_price": 55}).status_code == 503


@pytest.mark.parametrize("body", [
    {},                                              # nothing to record
    {"final_price": 0},                              # prices must be positive
    {"final_price": -5},
    {"final_price": 12.345},                         # more precision than DECIMAL(10,2)
    {"final_price": 100_000_000},                    # wider than DECIMAL(10,2)
    {"sold_at": "2026-09-27T14:30:00"},              # a sale date needs a sale price
    {"final_price": "lots"},
])
def test_invalid_outcome_is_rejected(monkeypatch, body):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with patch.object(app_module.repository, "record_outcome") as mock_record:
        resp = client.post("/estimates/7/outcome", json=body)

    assert resp.status_code == 422
    mock_record.assert_not_called()


def _post_outcome(body, *, recorded=None, updated=_OUTCOME_ROW, estimate_exists=True):
    recorded = recorded if recorded is not None else RecordedOutcome(_OUTCOME_ROW, is_created=True)
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "record_outcome", return_value=recorded) as mock_record,
        patch.object(app_module.repository, "update_outcome", return_value=updated) as mock_update,
        patch.object(app_module.repository, "estimate_exists", return_value=estimate_exists),
    ):
        resp = client.post("/estimates/7/outcome", json=body)
    return resp, mock_record, mock_update


def test_upsert_that_inserts_returns_201(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    resp, mock_record, mock_update = _post_outcome({"final_price": 55})

    assert resp.status_code == 201
    assert resp.json()["final_price"] == 55.0
    kwargs = mock_record.call_args.kwargs
    assert (kwargs["estimate_id"], kwargs["final_price"]) == (7, 55)
    mock_update.assert_not_called()


def test_upsert_that_updates_returns_200(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    resp, _, _ = _post_outcome(
        {"final_price": 60}, recorded=RecordedOutcome(_OUTCOME_ROW, is_created=False),
    )

    assert resp.status_code == 200


def test_sale_without_final_price_updates_the_existing_outcome(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    resp, mock_record, mock_update = _post_outcome({"sold_price": 50})

    assert resp.status_code == 200
    kwargs = mock_update.call_args.kwargs
    assert (kwargs["estimate_id"], kwargs["sold_price"]) == (7, 50)
    mock_record.assert_not_called()


def test_first_outcome_needs_a_final_price(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    resp, mock_record, _ = _post_outcome({"sold_price": 50}, updated=None)

    assert resp.status_code == 422
    assert "final_price" in resp.json()["detail"]
    mock_record.assert_not_called()


@pytest.mark.parametrize("body", [{"final_price": 55}, {"sold_price": 50}])
def test_outcome_for_missing_estimate_returns_404(monkeypatch, body):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "record_outcome", return_value=None),
        patch.object(app_module.repository, "update_outcome", return_value=None),
        patch.object(app_module.repository, "estimate_exists", return_value=False),
    ):
        resp = client.post("/estimates/7/outcome", json=body)

    assert resp.status_code == 404


def test_integrity_error_maps_to_409_without_internals(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    duplicate = pymysql.err.IntegrityError(1062, "Duplicate entry '7' for key 'secret_index'")
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "record_outcome", side_effect=duplicate),
    ):
        resp = client.post("/estimates/7/outcome", json={"final_price": 55})

    assert resp.status_code == 409
    assert "conflicts with data already saved" in resp.json()["detail"]
    assert "secret_index" not in resp.text
    assert "secret_index" in caplog.text  # full context stays in the server log


def test_operational_error_still_maps_to_503(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    lost = pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "record_outcome", side_effect=lost),
    ):
        resp = client.post("/estimates/7/outcome", json={"final_price": 55})

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Database unavailable"


def test_accuracy_report_separates_sold_price_accuracy_from_agreement(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    def row(basis, term, n):
        source = None if term is None else "vision_label"
        return {"basis": basis, "search_source": source, "search_term": term, "n": n,
                "mape_pct": 10.0,
                "median_ape_pct": 9.0, "median_abs_error": 4.0, "bias": 1.0}

    rows = [row("final_price", None, 4), row("final_price", "lamp", 4),
            row("sold_price", None, 2), row("sold_price", "lamp", 1), row("sold_price", "chair", 1)]
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "accuracy_by_search_term", return_value=rows),
    ):
        body = client.get("/analytics/accuracy").json()

    assert body["accuracy"]["basis"] == "sold_price"
    assert body["accuracy"]["overall"]["n"] == 2
    assert [r["search_term"] for r in body["accuracy"]["by_search_term"]] == ["lamp", "chair"]
    assert body["agreement_with_volunteer_price"]["basis"] == "final_price"
    assert body["agreement_with_volunteer_price"]["overall"]["n"] == 4


def test_accuracy_report_without_sales_has_no_overall(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "accuracy_by_search_term", return_value=[]),
    ):
        body = client.get("/analytics/accuracy").json()

    assert body["accuracy"] == {
        "basis": "sold_price", "min_n": 5, "overall": None, "by_search_term": [],
    }


def _accuracy_rows(*ns_by_term):
    return [
        {"basis": "sold_price", "search_source": None if term is None else "vision_label",
         "search_term": term, "n": n, "mape_pct": 10.0,
         "median_ape_pct": 9.0, "median_abs_error": 4.0, "bias": 1.0}
        for term, n in ns_by_term
    ]


def _get_accuracy(rows, query=""):
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "accuracy_by_search_term", return_value=rows),
    ):
        return client.get(f"/analytics/accuracy{query}")


def test_accuracy_rows_below_min_n_are_flagged_with_n_shown(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    rows = _accuracy_rows((None, 9), ("lamp", 5), ("chair", 4))

    accuracy = _get_accuracy(rows).json()["accuracy"]

    assert accuracy["min_n"] == 5
    assert (accuracy["overall"]["n"], accuracy["overall"]["is_below_min_n"]) == (9, False)
    assert [(r["search_term"], r["n"], r["is_below_min_n"]) for r in accuracy["by_search_term"]] == [
        ("lamp", 5, False), ("chair", 4, True),
    ]


def test_small_overall_sample_is_flagged_too(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    accuracy = _get_accuracy(_accuracy_rows((None, 3), ("lamp", 3))).json()["accuracy"]

    assert accuracy["overall"]["is_below_min_n"] is True


def test_min_n_can_be_lowered(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    accuracy = _get_accuracy(_accuracy_rows((None, 2), ("lamp", 2)), "?min_n=2").json()["accuracy"]

    assert accuracy["min_n"] == 2
    assert accuracy["by_search_term"][0]["is_below_min_n"] is False


@pytest.mark.parametrize("min_n", ["0", "-1", "lots"])
def test_invalid_min_n_is_rejected(monkeypatch, min_n):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    assert _get_accuracy([], f"?min_n={min_n}").status_code == 422


# -- label taxonomy -------------------------------------------------------------

_LABEL_ROWS = [
    {"label": "plastic", "canonical_term": None, "kind": "material"},
    {"label": "road bicycle", "canonical_term": "bicycle", "kind": "object"},
]


def _appraise_with_taxonomy(labels, **taxonomy_patch):
    with (
        patch.object(app_module._vision, "identify_item", return_value=labels),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE) as mock_search,
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module.repository, "label_taxonomy", **taxonomy_patch),
        patch.object(app_module.repository, "save_appraisal", return_value=_SAVED) as mock_save,
    ):
        resp = _post()
    return resp, mock_search.call_args.kwargs["taxonomy"], mock_save


def test_label_taxonomy_is_read_from_the_database(monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    labels = [ItemLabel("Plastic", 0.9), ItemLabel("Road bicycle", 0.8)]

    resp, taxonomy, mock_save = _appraise_with_taxonomy(labels, return_value=_LABEL_ROWS)

    assert resp.status_code == 200
    assert taxonomy is not FALLBACK_TAXONOMY
    # The category is the canonical term of the first searchable label
    assert resp.json()["item"] == "bicycle"
    assert mock_save.call_args.kwargs["category"] == "bicycle"
    assert mock_save.call_args.kwargs["description"] == "Plastic Road bicycle"  # Vision's raw labels


def test_unreadable_taxonomy_falls_back_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    lost = pymysql.err.OperationalError(2013, "Lost connection")

    resp, taxonomy, _ = _appraise_with_taxonomy(_LABELS, side_effect=lost)

    assert resp.status_code == 200
    assert taxonomy is FALLBACK_TAXONOMY
    assert "Label taxonomy unavailable" in caplog.text


def test_empty_taxonomy_table_falls_back_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    resp, taxonomy, _ = _appraise_with_taxonomy(_LABELS, return_value=[])

    assert taxonomy is FALLBACK_TAXONOMY
    assert "labels table has no valid rows" in caplog.text


_BAD_ROW = {"label": "broken", "canonical_term": None, "kind": "object"}


def test_invalid_taxonomy_row_is_skipped_and_the_rest_used(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    labels = [ItemLabel("Plastic", 0.9), ItemLabel("Road bicycle", 0.8)]

    resp, taxonomy, _ = _appraise_with_taxonomy(labels, return_value=[*_LABEL_ROWS, _BAD_ROW])

    assert taxonomy is not FALLBACK_TAXONOMY
    assert resp.json()["item"] == "bicycle"  # the valid rows still apply
    assert "Label taxonomy has an invalid row: broken" in caplog.text
    assert "no valid rows" not in caplog.text


def test_taxonomy_with_only_invalid_rows_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)

    _, taxonomy, _ = _appraise_with_taxonomy(_LABELS, return_value=[_BAD_ROW])

    assert taxonomy is FALLBACK_TAXONOMY
    assert "Label taxonomy has an invalid row: broken" in caplog.text
    assert "labels table has no valid rows" in caplog.text


def test_taxonomy_without_persistence_is_the_fallback():
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=_ESTIMATE) as mock_search,
    ):
        _post()

    assert mock_search.call_args.kwargs["taxonomy"] is FALLBACK_TAXONOMY


def test_response_reports_the_vision_label_behind_the_search():
    estimate = replace(
        _ESTIMATE, is_mock=False, source="browse_api", search_term="bicycle",
        search_source="vision_label", vision_label="Road bicycle",
    )
    with (
        patch.object(app_module._vision, "identify_item", return_value=_LABELS),
        patch.object(app_module._ebay, "search_labels", return_value=estimate),
    ):
        body = _post().json()

    assert (body["search_term"], body["vision_label"]) == ("bicycle", "Road bicycle")


# -- startup migration check -----------------------------------------------------

def _start_app(monkeypatch, **pending_patch):
    """Run the app's startup (lifespan) with pending_migrations patched."""
    monkeypatch.setattr(app_module, "_db_config", _FAKE_DB)
    with (
        patch.object(app_module, "connect", _fake_connect),
        patch.object(app_module, "pending_migrations", **pending_patch) as mock_pending,
        TestClient(app_module.app),
    ):
        pass
    return mock_pending


def test_pending_migrations_are_logged_as_an_error_at_startup(monkeypatch, caplog):
    _start_app(monkeypatch, return_value=["004_create_labels", "005_add_estimate_vision_label"])

    [record] = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "004_create_labels" in record.getMessage()
    assert "db.py init" in record.getMessage()


def test_current_schema_logs_no_error_at_startup(monkeypatch, caplog):
    mock_pending = _start_app(monkeypatch, return_value=[])

    mock_pending.assert_called_once()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_unreachable_database_at_startup_is_logged(monkeypatch, caplog):
    lost = pymysql.err.OperationalError(2003, "Can't connect")

    _start_app(monkeypatch, side_effect=lost)

    assert "Could not check database migrations" in caplog.text


def test_startup_check_is_skipped_without_persistence():
    with patch.object(app_module, "pending_migrations") as mock_pending, TestClient(app_module.app):
        pass

    mock_pending.assert_not_called()


# -- logging -------------------------------------------------------------------

def _configure(monkeypatch, level=None):
    if level is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", level)
    with patch.object(app_module.logging, "basicConfig") as basic_config:
        app_module._configure_logging()
    return basic_config.call_args.kwargs


@pytest.mark.parametrize("env,expected", [
    (None, logging.INFO),
    ("debug", logging.DEBUG),
    (" WARNING ", logging.WARNING),
])
def test_log_level_defaults_to_info_and_comes_from_env(monkeypatch, env, expected):
    assert _configure(monkeypatch, env)["level"] == expected


def test_unknown_log_level_falls_back_to_info_with_a_warning(monkeypatch, caplog):
    assert _configure(monkeypatch, "loud")["level"] == logging.INFO
    assert "LOG_LEVEL='loud' is not a log level" in caplog.text


def test_log_lines_carry_time_level_and_logger_name(monkeypatch):
    fmt = _configure(monkeypatch)["format"]

    for field in ("%(asctime)s", "%(levelname)s", "%(name)s", "%(message)s"):
        assert field in fmt


def test_http_client_request_lines_are_kept_out_of_info_logs(monkeypatch):
    _configure(monkeypatch)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
