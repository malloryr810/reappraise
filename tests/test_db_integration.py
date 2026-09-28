"""Integration tests against a real MySQL database.

Uses MYSQL_TEST_DATABASE (see .env.example). Skipped when it isn't configured
or the server isn't reachable, so the unit suite still runs anywhere. Every
table is truncated before each test.
"""

import statistics
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pymysql
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

import app as app_module
import repository
from db import (
    DbConfig, applied_migrations, connect, init_schema, migration_files, rollback_statements,
)
from ebay import (
    SEARCH_SOURCE_USER, SEARCH_SOURCE_VISION, SOURCE_BROWSE_API, SOURCE_MOCK,
    PriceEstimate, SampledListing,
)
from vision import ItemLabel

pytestmark = pytest.mark.integration

_TABLES = ("outcomes", "listings_sampled", "price_estimates", "items", "categories")


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
    source: str = SOURCE_BROWSE_API,
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
        ).item_id


def _count(config, table: str) -> int:
    with connect(config) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return cur.fetchone()["n"]


def _columns(config, table: str) -> set[str]:
    with connect(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )
        return {r["COLUMN_NAME"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def test_init_schema_applies_every_migration(db_config):
    assert "user_description" in _columns(db_config, "items")
    assert {"search_term", "search_source"} <= _columns(db_config, "price_estimates")
    with connect(db_config) as conn:
        assert applied_migrations(conn) == {version for version, _ in migration_files()}


def test_migration_upgrades_a_database_created_before_it(db_config):
    _save(db_config, "lamp", [10.0, 20.0])  # existing data must survive the upgrade
    # Rewind the test database to the pre-migration shape a real dev database has
    with connect(db_config) as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE items DROP COLUMN user_description")
        cur.execute("ALTER TABLE price_estimates DROP CHECK chk_estimates_search_source")
        cur.execute("ALTER TABLE price_estimates DROP COLUMN search_term, DROP COLUMN search_source")
        cur.execute("DELETE FROM schema_migrations")
    try:
        assert "user_description" not in _columns(db_config, "items")

        applied = init_schema(db_config)

        assert applied == [version for version, _ in migration_files()]
        assert "user_description" in _columns(db_config, "items")
        assert {"search_term", "search_source"} <= _columns(db_config, "price_estimates")
        assert _count(db_config, "items") == 1
        assert _count(db_config, "listings_sampled") == 2
    finally:
        init_schema(db_config)  # leave the test database migrated for later tests


def test_init_schema_is_idempotent(db_config):
    assert init_schema(db_config) == []
    assert init_schema(db_config) == []


def test_search_source_check_constraint_rejects_unknown_values(db_config):
    item_id = _save(db_config, "lamp", [10.0])
    with pytest.raises(pymysql.err.OperationalError), connect(db_config) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE price_estimates SET search_source = 'bogus' WHERE item_id = %s", (item_id,)
        )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def test_save_appraisal_writes_all_four_tables(db_config):
    with connect(db_config) as conn:
        saved = repository.save_appraisal(
            conn, category="camera", description="camera query",
            estimate=_estimate([10.0, 20.0, 30.0], condition="good", multiplier=0.7,
                               ids=["111", None, "333"]),
        )
    item_id = saved.item_id

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
    assert estimate["estimate_id"] == saved.estimate_id
    assert estimate["market_median"] == Decimal("20.00")
    assert estimate["estimated_price"] == Decimal("14.00")
    assert estimate["source"] == SOURCE_BROWSE_API


def test_user_description_and_search_term_are_persisted(db_config):
    estimate = replace(
        _estimate([900.0, 1500.0, 2100.0]),
        search_term="Trek Emonda SL 5", search_source=SEARCH_SOURCE_USER,
    )
    with connect(db_config) as conn:
        item_id = repository.save_appraisal(
            conn, category="bicycle", description="bicycle bicycle tire wheel",
            estimate=estimate, user_description="Trek Emonda SL 5",
        ).item_id
        detail = repository.get_item_detail(conn, item_id)
        [history] = repository.recent_estimates(conn, limit=5)

    assert detail.item["user_description"] == "Trek Emonda SL 5"
    assert detail.item["description"] == "bicycle bicycle tire wheel"  # Vision kept apart
    assert detail.estimates[0]["search_term"] == "Trek Emonda SL 5"
    assert detail.estimates[0]["search_source"] == SEARCH_SOURCE_USER
    assert (history["user_description"], history["search_source"]) == (
        "Trek Emonda SL 5", SEARCH_SOURCE_USER,
    )


def test_vision_only_appraisal_leaves_user_description_null(db_config):
    item_id = _save(db_config, "lamp", [10.0])

    with connect(db_config) as conn:
        detail = repository.get_item_detail(conn, item_id)
    assert detail.item["user_description"] is None


def test_recompute_keeps_the_original_search_term(db_config):
    estimate = replace(_estimate([10.0, 20.0]), search_term="lamp", search_source=SEARCH_SOURCE_VISION)
    with connect(db_config) as conn:
        item_id = repository.save_appraisal(
            conn, category="lamp", description="lamp", estimate=estimate,
        ).item_id
        repository.recompute_estimate(conn, item_id, multiplier=0.8)
        detail = repository.get_item_detail(conn, item_id)

    assert [(e["search_term"], e["search_source"]) for e in detail.estimates] == [
        ("lamp", SEARCH_SOURCE_VISION), ("lamp", SEARCH_SOURCE_VISION),
    ]


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
    assert detail.estimates[0]["source"] == SOURCE_BROWSE_API


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


def test_price_range_by_category_ranks_widest_spread_first(db_config):
    _save(db_config, "bicycle", [106.0, 470.0, 2500.0])
    _save(db_config, "bicycle", [200.0, 300.0])       # second item widens nothing
    _save(db_config, "wallet", [1.69, 12.62, 149.99])
    _save(db_config, "lamp", [10.0])                  # single listing — range 0
    _save(db_config, "toy", [999.0], source=SOURCE_MOCK)  # mock stores no listings

    with connect(db_config) as conn:
        rows = repository.price_range_by_category(conn, limit=10)

    assert [r["category"] for r in rows] == ["bicycle", "wallet", "lamp"]
    bicycle, wallet, lamp = rows
    assert (bicycle["item_count"], bicycle["listing_count"]) == (2, 5)
    assert (bicycle["low_price"], bicycle["high_price"]) == (Decimal("106.00"), Decimal("2500.00"))
    assert bicycle["price_range"] == Decimal("2394.00")
    assert bicycle["high_to_low_ratio"] == Decimal("23.58")
    assert wallet["high_to_low_ratio"] == Decimal("88.75")
    assert [r["range_rank"] for r in rows] == [1, 2, 3]
    assert lamp["price_range"] == Decimal("0.00")


def test_price_range_by_category_returns_ties_and_respects_limit(db_config):
    _save(db_config, "lamp", [10.0, 20.0])
    _save(db_config, "drill", [50.0, 60.0])
    _save(db_config, "toy", [1.0, 2.0])

    with connect(db_config) as conn:
        tied = repository.price_range_by_category(conn, limit=10)
        limited = repository.price_range_by_category(conn, limit=1)

    assert [(r["category"], r["range_rank"]) for r in tied] == [
        ("drill", 1), ("lamp", 1), ("toy", 3),
    ]
    assert len(limited) == 1


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
        patch.object(app_module._ebay, "search_labels", return_value=estimate),
    ):
        resp = client.post("/appraise", files={"file": ("x.jpg", b"\xff\xd8\xff", "image/jpeg")})

    assert resp.status_code == 200
    item_id = resp.json()["item_id"]
    assert isinstance(item_id, int)
    estimate_id = resp.json()["estimate_id"]

    history = client.get("/history").json()
    assert (history[0]["item_id"], history[0]["estimate_id"]) == (item_id, estimate_id)
    assert history[0]["category"] == "camera"
    assert history[0]["estimated_price"] == 72.0
    assert history[0]["sample_size"] == 3

    detail = client.get(f"/history/{item_id}").json()
    assert detail["description"] == "camera lens"
    assert [l["ebay_listing_id"] for l in detail["listings"]] == ["a1", "a2", "a3"]
    assert client.get("/history/999999").status_code == 404

    assert client.get("/analytics/categories").json()[0]["category"] == "camera"
    assert client.get("/analytics/top-category").json() == [{"category": "camera", "item_count": 1}]
    assert history[0]["user_description"] is None
    [price_range] = client.get("/analytics/price-range").json()
    assert (price_range["category"], price_range["price_range"]) == ("camera", 310.0)


def test_appraise_with_description_round_trips_through_history(db_config, monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", db_config)
    estimate = replace(
        _estimate([900.0, 1500.0, 2100.0]),
        search_term="Trek Emonda SL 5", search_source=SEARCH_SOURCE_USER,
    )
    client = TestClient(app_module.app)

    with (
        patch.object(app_module._vision, "identify_item",
                     return_value=[ItemLabel("bicycle", 0.95), ItemLabel("wheel", 0.8)]),
        patch.object(app_module._ebay, "search_labels", return_value=estimate),
    ):
        resp = client.post(
            "/appraise",
            files={"file": ("x.jpg", b"\xff\xd8\xff", "image/jpeg")},
            data={"user_description": "Trek Emonda SL 5"},
        )

    item_id = resp.json()["item_id"]
    [row] = client.get("/history").json()
    assert row["item_id"] == item_id
    assert row["category"] == "bicycle"
    assert row["user_description"] == "Trek Emonda SL 5"
    assert (row["search_term"], row["search_source"]) == ("Trek Emonda SL 5", "user_description")
    detail = client.get(f"/history/{item_id}").json()
    assert detail["user_description"] == "Trek Emonda SL 5"
    assert detail["estimates"][0]["search_source"] == "user_description"


# ---------------------------------------------------------------------------
# Migrations from an empty database and from one at 002
# ---------------------------------------------------------------------------

def _tables(config) -> set[str]:
    with connect(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        )
        return {r["TABLE_NAME"] for r in cur.fetchall()}


def _rewind_to(config, version: str) -> list[str]:
    """Undo every migration after `version` by running its -- rollback: note.

    Returns the rewound versions, oldest first. Running the notes here is what
    keeps them honest: a wrong rollback note fails this test.
    """
    later = [(v, path) for v, path in migration_files() if v > version]
    with connect(config) as conn, conn.cursor() as cur:
        for v, path in reversed(later):
            for stmt in rollback_statements(path):
                cur.execute(stmt)
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (v,))
    return [v for v, _ in later]


def test_init_schema_builds_an_empty_database(db_config):
    tables = _tables(db_config)
    with connect(db_config) as conn, conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in tables:
            cur.execute(f"DROP TABLE `{table}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    try:
        assert _tables(db_config) == set()

        applied = init_schema(db_config)

        assert applied == [version for version, _ in migration_files()]
        assert _tables(db_config) == tables
    finally:
        init_schema(db_config)


def test_migrations_upgrade_a_database_at_002(db_config):
    _save(db_config, "lamp", [10.0, 20.0])  # existing data must survive the upgrade
    rewound = _rewind_to(db_config, "002_add_estimate_search_term")
    try:
        assert "003_create_outcomes" in rewound
        assert "outcomes" not in _tables(db_config)

        assert init_schema(db_config) == rewound

        assert "outcomes" in _tables(db_config)
        assert _count(db_config, "items") == 1
        assert _count(db_config, "listings_sampled") == 2
    finally:
        init_schema(db_config)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

def _priced(config, term: str, estimated_price: float, *, source: str = SOURCE_BROWSE_API) -> int:
    """Save an appraisal whose estimate is exactly `estimated_price`; returns estimate_id.

    One listing at estimated_price / 0.6, so the fair-condition estimate lands on it.
    """
    estimate = replace(
        _estimate([round(estimated_price / 0.6, 2)], source=source),
        search_term=term, search_source=SEARCH_SOURCE_VISION,
    )
    with connect(config) as conn:
        return repository.save_appraisal(
            conn, category=term, description=term, estimate=estimate,
        ).estimate_id


def _outcome(config, estimate_id: int, final_price: str, sold_price: str | None = None):
    with connect(config) as conn:
        return repository.record_outcome(
            conn, estimate_id=estimate_id, final_price=Decimal(final_price),
            sold_price=Decimal(sold_price) if sold_price else None,
        )


def test_record_outcome_inserts_and_returns_the_row(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)

    recorded = _outcome(db_config, estimate_id, "55.00")

    assert recorded.is_created is True
    row = recorded.row
    assert row["estimate_id"] == estimate_id
    assert row["final_price"] == Decimal("55.00")
    assert (row["sold_price"], row["sold_at"]) == (None, None)
    assert row["created_at"] is not None


def test_record_outcome_returns_none_for_missing_estimate(db_config):
    assert _outcome(db_config, 999, "10.00") is None
    assert _count(db_config, "outcomes") == 0


def test_second_record_on_the_same_estimate_updates_it(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)
    _outcome(db_config, estimate_id, "55.00", "50.00")

    again = _outcome(db_config, estimate_id, "58.00")

    assert again.is_created is False
    # final_price is replaced; a sale already recorded is kept
    assert (again.row["final_price"], again.row["sold_price"]) == (Decimal("58.00"), Decimal("50.00"))
    assert _count(db_config, "outcomes") == 1


def test_recording_identical_values_again_is_still_an_update(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)
    _outcome(db_config, estimate_id, "55.00")

    # MySQL reports 0 affected rows when nothing changed; that is not an insert
    assert _outcome(db_config, estimate_id, "55.00").is_created is False


def test_unique_key_still_rejects_a_duplicate_plain_insert(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)
    _outcome(db_config, estimate_id, "55.00")

    with pytest.raises(pymysql.err.IntegrityError), connect(db_config) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outcomes (estimate_id, final_price) VALUES (%s, 1)", (estimate_id,)
        )


def test_update_outcome_records_a_sale_and_keeps_the_final_price(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)
    _outcome(db_config, estimate_id, "55.00")
    sold_at = datetime(2026, 9, 27, 14, 30)

    with connect(db_config) as conn:
        row = repository.update_outcome(
            conn, estimate_id=estimate_id, sold_price=Decimal("50.00"), sold_at=sold_at,
        )

    assert (row["final_price"], row["sold_price"], row["sold_at"]) == (
        Decimal("55.00"), Decimal("50.00"), sold_at,
    )


def test_update_outcome_returns_none_without_an_outcome(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)

    with connect(db_config) as conn:
        assert repository.update_outcome(conn, estimate_id=estimate_id, sold_price=Decimal("5")) is None


def test_outcome_prices_must_be_positive(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)

    with pytest.raises(pymysql.err.OperationalError):
        _outcome(db_config, estimate_id, "0.00")


def test_deleting_an_item_deletes_its_outcome(db_config):
    estimate_id = _priced(db_config, "lamp", 60.0)
    _outcome(db_config, estimate_id, "55.00")

    with connect(db_config) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM items")

    assert _count(db_config, "outcomes") == 0


# ---------------------------------------------------------------------------
# Accuracy analytics
# ---------------------------------------------------------------------------

def _metrics(row) -> tuple:
    return (row["basis"], row["search_term"], row["n"], row["mape_pct"],
            row["median_ape_pct"], row["median_abs_error"], row["bias"])


def test_accuracy_metrics_match_hand_computed_values(db_config):
    # estimate, final, sold  →  error vs sold (APE)       error vs final (APE)
    a = _priced(db_config, "lamp", 60.0)   # 55, 50  →  +10 (20%)          +5 (9.0909%)
    b = _priced(db_config, "lamp", 30.0)   # 40, 40  →  -10 (25%)         -10 (25%)
    c = _priced(db_config, "chair", 90.0)  # 75, 60  →  +30 (50%)         +15 (20%)
    d = _priced(db_config, "chair", 24.0)  # 30, --  →  not sold           -6 (20%)
    mock = _priced(db_config, "toy", 60.0, source=SOURCE_MOCK)  # excluded entirely
    _outcome(db_config, a, "55.00", "50.00")
    _outcome(db_config, b, "40.00", "40.00")
    _outcome(db_config, c, "75.00", "60.00")
    _outcome(db_config, d, "30.00")
    _outcome(db_config, mock, "1.00", "1.00")

    with connect(db_config) as conn:
        rows = repository.accuracy_by_search_term(conn)

    D = Decimal
    assert [_metrics(r) for r in rows] == [
        # final_price: overall APEs 9.09, 20, 20, 25 → median 20; |errors| 5, 6, 10, 15 → median 8
        ("final_price", None,    4, D("18.52"), D("20.00"), D("8.00"),  D("1.00")),
        ("final_price", "chair", 2, D("20.00"), D("20.00"), D("10.50"), D("4.50")),
        ("final_price", "lamp",  2, D("17.05"), D("17.05"), D("7.50"),  D("-2.50")),
        # sold_price: overall APEs 20, 25, 50 → MAPE 95/3; |errors| 10, 10, 30 → median 10
        ("sold_price",  None,    3, D("31.67"), D("25.00"), D("10.00"), D("10.00")),
        ("sold_price",  "lamp",  2, D("22.50"), D("22.50"), D("10.00"), D("0.00")),
        ("sold_price",  "chair", 1, D("50.00"), D("50.00"), D("30.00"), D("30.00")),
    ]


def test_accuracy_counts_each_item_once_using_its_latest_estimate(db_config):
    first = _priced(db_config, "lamp", 60.0)  # listing at 100
    with connect(db_config) as conn:
        item_id = repository.recent_estimates(conn, limit=1)[0]["item_id"]
        latest = repository.recompute_estimate(conn, item_id, multiplier=0.9)["estimate_id"]
    _outcome(db_config, first, "60.00", "60.00")   # superseded — would be 0% error
    _outcome(db_config, latest, "60.00", "60.00")  # 90 vs 60 → 50%

    with connect(db_config) as conn:
        rows = repository.accuracy_by_search_term(conn)

    overall = next(r for r in rows if r["basis"] == "sold_price" and r["search_term"] is None)
    assert (overall["n"], overall["mape_pct"]) == (1, Decimal("50.00"))


def _outcome_checks(config) -> list[str]:
    with connect(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'outcomes' "
            "AND CONSTRAINT_TYPE = 'CHECK'"
        )
        return [r["CONSTRAINT_NAME"] for r in cur.fetchall()]


def test_accuracy_skips_zero_prices_even_if_checks_are_not_enforced(db_config):
    kept = _priced(db_config, "lamp", 60.0)
    zeroed = _priced(db_config, "lamp", 30.0)
    _outcome(db_config, kept, "55.00", "50.00")    # +10 → 20%
    _outcome(db_config, zeroed, "40.00", "40.00")  # sale price set to 0 below
    checks = _outcome_checks(db_config)
    assert len(checks) == 2

    with connect(db_config) as conn, conn.cursor() as cur:
        for name in checks:
            cur.execute(f"ALTER TABLE outcomes ALTER CHECK `{name}` NOT ENFORCED")
        cur.execute("UPDATE outcomes SET sold_price = 0 WHERE estimate_id = %s", (zeroed,))
    try:
        with connect(db_config) as conn:
            rows = repository.accuracy_by_search_term(conn)
    finally:
        with connect(db_config) as conn, conn.cursor() as cur:
            cur.execute("UPDATE outcomes SET sold_price = NULL WHERE sold_price = 0")
            for name in checks:
                cur.execute(f"ALTER TABLE outcomes ALTER CHECK `{name}` ENFORCED")

    overall = next(r for r in rows if r["basis"] == "sold_price" and r["search_term"] is None)
    assert (overall["n"], overall["mape_pct"], overall["median_ape_pct"]) == (
        1, Decimal("20.00"), Decimal("20.00"),
    )


def test_accuracy_is_empty_without_outcomes(db_config):
    _priced(db_config, "lamp", 60.0)

    with connect(db_config) as conn:
        assert repository.accuracy_by_search_term(conn) == []


def test_outcome_endpoint_and_accuracy_report_round_trip(db_config, monkeypatch):
    monkeypatch.setattr(app_module, "_db_config", db_config)
    client = TestClient(app_module.app)
    estimate_id = _priced(db_config, "lamp", 60.0)
    url = f"/estimates/{estimate_id}/outcome"

    created = client.post(url, json={"final_price": 55})
    again = client.post(url, json={"final_price": 55})
    sold = client.post(url, json={"sold_price": 50, "sold_at": "2026-09-27T14:30:00"})

    assert created.status_code == 201
    assert again.status_code == 200  # same estimate again is an update, not an error
    assert (created.json()["final_price"], created.json()["sold_price"]) == (55.0, None)
    assert sold.status_code == 200
    assert (sold.json()["final_price"], sold.json()["sold_price"]) == (55.0, 50.0)
    assert sold.json()["sold_at"] == "2026-09-27T14:30:00"
    assert client.post("/estimates/999999/outcome", json={"final_price": 5}).status_code == 404
    assert client.post("/estimates/999999/outcome", json={"sold_price": 5}).status_code == 404
    unsold_id = _priced(db_config, "chair", 30.0)
    first_without_price = client.post(f"/estimates/{unsold_id}/outcome", json={"sold_price": 5})
    assert first_without_price.status_code == 422
    assert _count(db_config, "outcomes") == 1

    report = client.get("/analytics/accuracy?min_n=1").json()
    assert report["accuracy"]["basis"] == "sold_price"
    assert report["accuracy"]["overall"] == {
        "search_term": None, "n": 1, "mape_pct": 20.0, "median_ape_pct": 20.0,
        "median_abs_error": 10.0, "bias": 10.0, "is_below_min_n": False,
    }
    assert client.get("/analytics/accuracy").json()["accuracy"]["overall"]["is_below_min_n"]
    assert [r["search_term"] for r in report["accuracy"]["by_search_term"]] == ["lamp"]
    agreement = report["agreement_with_volunteer_price"]
    assert (agreement["basis"], agreement["overall"]["mape_pct"]) == ("final_price", 9.09)
