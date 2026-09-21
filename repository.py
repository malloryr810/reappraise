# Persistence for appraisals. Every function takes an open connection from
# db.connect(), so the caller controls the transaction boundary; SQL text lives
# in sql/queries.sql and is looked up by name.

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pymysql.connections import Connection

from db import query
from ebay import PriceEstimate

# Match the VARCHAR widths in sql/schema.sql so long Vision labels are trimmed
# rather than rejected by strict-mode MySQL.
_CATEGORY_MAX = 100
_DESCRIPTION_MAX = 255

Row = dict[str, Any]


@dataclass(frozen=True)
class ItemDetail:
    item: Row
    estimates: list[Row]
    listings: list[Row]


def save_appraisal(
    conn: Connection, *, category: str, description: str, estimate: PriceEstimate
) -> int:
    """Persist one appraisal (category, item, sampled listings, estimate).

    Returns the new item_id. The caller's `with connect(...)` block makes this
    all-or-nothing.
    """
    with conn.cursor() as cur:
        cur.execute(query("upsert_category"), {"name": category.strip()[:_CATEGORY_MAX]})
        category_id = cur.lastrowid

        cur.execute(
            query("insert_item"),
            {
                "category_id": category_id,
                "description": description[:_DESCRIPTION_MAX],
                "condition_label": estimate.condition,
                "condition_multiplier": estimate.multiplier_used,
            },
        )
        item_id = cur.lastrowid

        if estimate.listings:
            cur.executemany(
                query("insert_listing"),
                [
                    {
                        "item_id": item_id,
                        "ebay_listing_id": listing.ebay_listing_id,
                        "sampled_price": listing.price,
                    }
                    for listing in estimate.listings
                ],
            )

        cur.execute(
            query("insert_estimate"),
            {
                "item_id": item_id,
                "market_median": estimate.market_median,
                "estimated_price": estimate.estimated_resale_value,
                "source": estimate.source,
            },
        )
    return item_id


def recent_estimates(conn: Connection, limit: int = 20) -> list[Row]:
    return _fetch_all(conn, "recent_estimates", limit=limit)


def get_item_detail(conn: Connection, item_id: int) -> ItemDetail | None:
    item = _fetch_one(conn, "get_item", item_id=item_id)
    if item is None:
        return None
    return ItemDetail(
        item=item,
        estimates=_fetch_all(conn, "item_estimates", item_id=item_id),
        listings=_fetch_all(conn, "item_listings", item_id=item_id),
    )


def recompute_estimate(
    conn: Connection, item_id: int, multiplier: float | None = None
) -> Row | None:
    """Re-derive an item's estimate from its stored listings — no eBay call.

    The median is computed in SQL (see median_from_listings). `multiplier`
    overrides the item's stored condition multiplier, e.g. after a pricing
    policy change. Inserts a new price_estimates row (history is kept) and
    returns it, or None if the item doesn't exist or has no stored listings
    (mock-priced items never do).
    """
    detail = get_item_detail(conn, item_id)
    if detail is None or not detail.estimates:
        return None

    median_row = _fetch_one(conn, "median_from_listings", item_id=item_id)
    if median_row is None or median_row["market_median"] is None:
        return None

    market_median: Decimal = median_row["market_median"]
    factor = Decimal(str(multiplier)) if multiplier is not None else detail.item["condition_multiplier"]
    estimated = (market_median * factor).quantize(Decimal("0.01"))
    # Listings came from the same place as the original estimate
    source = detail.estimates[-1]["source"]

    with conn.cursor() as cur:
        cur.execute(
            query("insert_estimate"),
            {
                "item_id": item_id,
                "market_median": market_median,
                "estimated_price": estimated,
                "source": source,
            },
        )
        estimate_id = cur.lastrowid
    return {
        "estimate_id": estimate_id,
        "item_id": item_id,
        "market_median": market_median,
        "estimated_price": estimated,
        "sample_size": median_row["sample_size"],
        "source": source,
    }


# -- analytics ----------------------------------------------------------------


def avg_price_by_category(conn: Connection) -> list[Row]:
    return _fetch_all(conn, "avg_price_by_category")


def below_category_market(
    conn: Connection, ratio: float = 0.5, min_peers: int = 3, limit: int = 50
) -> list[Row]:
    return _fetch_all(
        conn, "below_category_market", ratio=ratio, min_peers=min_peers, limit=limit
    )


def top_category_by_volume(conn: Connection) -> list[Row]:
    return _fetch_all(conn, "top_category_by_volume")


# -- helpers ------------------------------------------------------------------


def _fetch_all(conn: Connection, name: str, **params: Any) -> list[Row]:
    with conn.cursor() as cur:
        cur.execute(query(name), params)
        return list(cur.fetchall())


def _fetch_one(conn: Connection, name: str, **params: Any) -> Row | None:
    with conn.cursor() as cur:
        cur.execute(query(name), params)
        return cur.fetchone()
