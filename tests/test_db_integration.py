"""Integration tests against a real MySQL database.

Uses MYSQL_TEST_DATABASE (see .env.example). Skipped when it isn't configured
or the server isn't reachable, so the unit suite still runs anywhere. Every
table is truncated before each test.
"""

import statistics
from decimal import Decimal
from unittest.mock import patch

import pymysql
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

import app as app_module
import repository
from db import DbConfig, connect, init_schema
from ebay import SOURCE_BROWSE_API, SOURCE_MOCK, SOURCE_SOLD_SCRAPE, PriceEstimate, SampledListing
from vision import ItemLabel

pytestmark = pytest.mark.integration

_TABLES = ("listings_sampled", "price_estimates", "items", "categories")


@pytest.fixture(scope="module")
def db_config() -> DbConfig:
    load_dotenv()
    config = DbConfig.from_env("MYSQL_TEST_DATABASE")
    if config is None:
        pytest.skip("MYSQL_TEST_DATABASE not set")
    app_config = DbConfig.from_env()
    if app_config is not None and app_config.database == config.database:
        pytest.fail("MYSQL_TEST_DATABASE must differ from MYSQL_DATABASE — tests truncate tables")
    try:
        init_schema(config)
    except pymysql.err.OperationalError as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    return config


@pytest.fixture(autouse=True)
def clean_tables(db_config):
    with connect(db_config) as conn, conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in _TABLES:
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def _estimate(
    prices: list[float],
    *,
    condition: str = "fair",
    multiplier: float = 0.6,
    source: str = SOURCE_SOLD_SCRAPE,
    ids: list[str | None] | None = None,
) -> PriceEstimate:
    ids = ids or [f"ebay-{i}" for i in range(len(prices))]
    median = round(statistics.median(prices), 2)
    return PriceEstimate(
        low=min(prices), market_median=median,
        estimated_resale_value=round(median * multiplier, 2), high=max(prices),
        currency="USD", sample_size=len(prices), is_mock=source == SOURCE_MOCK,
        condition=condition, multiplier_used=multiplier, source=source,
        listings=() if source == SOURCE_MOCK
        else tuple(SampledListing(p, i) for p, i in zip(prices, ids)),
    )


def _save(config, category, prices, **kwargs) -> int:
    with connect(config) as conn:
        return repository.save_appraisal(
            conn, category=category, description=f"{category} query",
            estimate=_estimate(prices, **kwargs),
        )


def _count(config, table: str) -> int:
    with connect(config) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return cur.fetchone()["n"]


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def test_save_appraisal_writes_all_four_tables(db_config):
    item_id = _save(db_config, "camera", [10.0, 20.0, 30.0], condition="good",
                    multiplier=0.7, ids=["111", None, "333"])

    with connect(db_config) as conn:
        detail = repository.get_item_detail(conn, item_id)

    assert detail.item["category"] == "camera"
    assert detail.item["description"] == "camera query"
    assert detail.item["condition_label"] == "good"
    assert detail.item["condition_multiplier"] == Decimal("0.70")
    assert [(r["sampled_price"], r["ebay_listing_id"]) for r in detail.listings] == [
        (Decimal("10.00"), "111"), (Decimal("20.00"), None), (Decimal("30.00"), "333"),
    ]
    [estimate] = detail.estimates
    assert estimate["market_median"] == Decimal("20.00")
    assert estimate["estimated_price"] == Decimal("14.00")
    assert estimate["source"] == SOURCE_SOLD_SCRAPE


def test_category_is_reused_across_appraisals(db_config):
    first = _save(db_config, "lamp", [5.0])
    second = _save(db_config, "lamp", [7.0])
    _save(db_config, "drill", [40.0])

    assert _count(db_config, "categories") == 2
    with connect(db_config) as conn, conn.cursor() as cur:
        cur.execute("SELECT item_id, category_id FROM items ORDER BY item_id")
        rows = cur.fetchall()
    assert rows[0]["item_id"] == first and rows[1]["item_id"] == second
    assert rows[0]["category_id"] == rows[1]["category_id"] != rows[2]["category_id"]


def test_mock_appraisal_stores_estimate_without_listings(db_config):
    item_id = _save(db_config, "toy", [5.0, 15.0, 45.0], source=SOURCE_MOCK)

    with connect(db_config) as conn:
        detail = repository.get_item_detail(conn, item_id)
    assert detail.listings == []
    assert detail.estimates[0]["source"] == SOURCE_MOCK


def test_failed_save_rolls_back_entire_appraisal(db_config):
    # 'bogus' violates the CHECK constraint on price_estimates.source, which is
    # the last insert — the item and listings before it must not survive
    with pytest.raises(pymysql.err.OperationalError):
        _save(db_config, "camera", [10.0, 20.0], source="bogus")

    for table in _TABLES:
        assert _count(db_config, table) == 0, table


def test_long_labels_are_truncated_to_column_width(db_config):
    item_id = _save(db_config, "x" * 150, [1.0])

    with connect(db_config) as conn:
        detail = repository.get_item_detail(conn, item_id)
    assert len(detail.item["category"]) == 100


# ---------------------------------------------------------------------------
# History read path
# ---------------------------------------------------------------------------

def test_recent_estimates_newest_first_with_listing_spread(db_config):
    older = _save(db_config, "lamp", [5.0, 9.0, 12.0])
    newer = _save(db_config, "toy", [1.0], source=SOURCE_MOCK)

    with connect(db_config) as conn:
        rows = repository.recent_estimates(conn, limit=10)

    assert [r["item_id"] for r in rows] == [newer, older]
    mock_row, lamp_row = rows
    assert (lamp_row["category"], lamp_row["sample_size"]) == ("lamp", 3)
    assert (lamp_row["low"], lamp_row["high"]) == (Decimal("5.00"), Decimal("12.00"))
    assert (mock_row["sample_size"], mock_row["low"]) == (0, None)


def test_recent_estimates_respects_limit(db_config):
    for i in range(5):
        _save(db_config, f"cat{i}", [float(i + 1)])

    with connect(db_config) as conn:
        assert len(repository.recent_estimates(conn, limit=3)) == 3


def test_get_item_detail_returns_none_for_missing_item(db_config):
    with connect(db_config) as conn:
        assert repository.get_item_detail(conn, 999) is None


# ---------------------------------------------------------------------------
# Recompute from stored listings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prices", [
    [30.0, 10.0, 20.0],                 # odd count
    [40.0, 10.0, 30.0, 20.0],           # even count — average of middle two
    [99.99],                            # single listing
    [5.0, 5.0, 5.0, 250.0, 7.5, 12.0],  # duplicates + outlier
])
def test_sql_median_matches_python_median(db_config, prices):
    item_id = _save(db_config, "lamp", prices)

    with connect(db_config) as conn:
        result = repository.recompute_estimate(conn, item_id)

    assert result["market_median"] == Decimal(str(round(statistics.median(prices), 2)))
    assert result["sample_size"] == len(prices)


def test_recompute_adds_new_estimate_with_override_multiplier(db_config):
    item_id = _save(db_config, "drill", [10.0, 20.0, 30.0], multiplier=0.6)

    with connect(db_config) as conn:
        result = repository.recompute_estimate(conn, item_id, multiplier=0.75)
    with connect(db_config) as conn:
        detail = repository.get_item_detail(conn, item_id)

    assert result["estimated_price"] == Decimal("15.00")
    assert len(detail.estimates) == 2
    assert detail.estimates[0]["estimated_price"] == Decimal("15.00")  # newest first
    assert detail.estimates[1]["estimated_price"] == Decimal("12.00")
    assert detail.estimates[0]["source"] == SOURCE_SOLD_SCRAPE


def test_recompute_returns_none_without_stored_listings(db_config):
    mock_item = _save(db_config, "toy", [1.0], source=SOURCE_MOCK)

    with connect(db_config) as conn:
        assert repository.recompute_estimate(conn, mock_item) is None
        assert repository.recompute_estimate(conn, 999) is None


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def test_avg_price_by_category_excludes_mock_and_uses_latest_estimate(db_config):
    _save(db_config, "lamp", [10.0])                         # est 6.00
    lamp2 = _save(db_config, "lamp", [20.0])                 # est 12.00
    _save(db_config, "drill", [100.0], source=SOURCE_BROWSE_API)  # est 60.00
    _save(db_config, "toy", [999.0], source=SOURCE_MOCK)     # excluded
    with connect(db_config) as conn:
        repository.recompute_estimate(conn, lamp2, multiplier=0.8)  # lamp2 -> 16.00

    with connect(db_config) as conn:
        rows = repository.avg_price_by_category(conn)

    assert [r["category"] for r in rows] == ["drill", "lamp"]
    drill, lamp = rows
    assert (drill["item_count"], drill["avg_estimated_price"]) == (1, Decimal("60.00"))
    # (6 + 16) / 2 — the superseded 12.00 estimate isn't double-counted
    assert (lamp["item_count"], lamp["avg_estimated_price"]) == (2, Decimal("11.00"))
    assert lamp["avg_market_median"] == Decimal("15.00")


def test_below_category_market_flags_items_under_ratio(db_config):
    for price in (100.0, 110.0, 90.0):
        _save(db_config, "camera", [price])
    cheap = _save(db_config, "camera", [20.0])  # category avg = 80 → ratio 0.25
    _save(db_config, "lamp", [10.0])            # only one lamp — below min_peers
    _save(db_config, "camera", [1.0], source=SOURCE_MOCK)  # excluded

    with connect(db_config) as conn:
        rows = repository.below_category_market(conn, ratio=0.5, min_peers=3)

    assert [r["item_id"] for r in rows] == [cheap]
    assert rows[0]["category_avg_median"] == Decimal("80.00")
    assert rows[0]["ratio_to_category"] == Decimal("0.25")


def test_top_category_by_volume_returns_all_ties(db_config):
    for category in ("lamp", "lamp", "drill", "drill", "toy"):
        _save(db_config, category, [10.0])

    with connect(db_config) as conn:
        rows = repository.top_category_by_volume(conn)

    assert rows == [{"category": "drill", "item_count": 2}, {"category": "lamp", "item_count": 2}]


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------

def test_appraise_persists_and_history_reads_it_back(db_config, monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", db_config)
    estimate = _estimate([40.0, 120.0, 350.0], ids=["a1", "a2", "a3"])
    client = TestClient(app_module.app)

    with (
        patch.object(app_module._vision, "identify_item",
                     return_value=[ItemLabel("camera", 0.95), ItemLabel("lens", 0.8)]),
        patch.object(app_module._ebay, "search_prices", return_value=estimate),
    ):
        resp = client.post("/appraise", files={"file": ("x.jpg", b"\xff\xd8\xff", "image/jpeg")})

    assert resp.status_code == 200
    item_id = resp.json()["item_id"]
    assert isinstance(item_id, int)

    history = client.get("/history").json()
    assert history[0]["item_id"] == item_id
    assert history[0]["category"] == "camera"
    assert history[0]["estimated_price"] == 72.0
    assert history[0]["sample_size"] == 3

    detail = client.get(f"/history/{item_id}").json()
    assert detail["description"] == "camera lens"
    assert [l["ebay_listing_id"] for l in detail["listings"]] == ["a1", "a2", "a3"]
    assert client.get("/history/999999").status_code == 404

    assert client.get("/analytics/categories").json()[0]["category"] == "camera"
    assert client.get("/analytics/top-category").json() == [{"category": "camera", "item_count": 1}]
    assert client.get("/analytics/below-market").json() == []
