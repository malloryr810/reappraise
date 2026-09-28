from dotenv import load_dotenv

load_dotenv()  # must run before EbayClient reads env vars

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
import pymysql
from pymysql.connections import Connection

import repository
from db import DbConfig, connect
from ebay import CONDITION_MULTIPLIERS, EbayClient, PriceEstimate, specific_labels
from vision import VisionClient

logger = logging.getLogger("reappraise")

# Matches items.user_description VARCHAR(255); longer input is rejected, not trimmed
USER_DESCRIPTION_MAX = 255

# Matches outcomes.final_price / sold_price DECIMAL(10,2); zero is rejected
# because percentage error divides by the actual price
Price = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]

# What an estimate is compared with in /analytics/accuracy. Only a sale price
# measures accuracy; the volunteer's price is set after seeing the estimate.
ACCURACY_BASIS = "sold_price"
AGREEMENT_BASIS = "final_price"
# Fewer comparisons than this and a percentage error is mostly noise; such rows
# are flagged rather than hidden, and always carry their n
ACCURACY_MIN_N_DEFAULT = 5


class LabelOut(BaseModel):
    name: str
    confidence: float


class PriceOut(BaseModel):
    low: float
    market_median: float
    estimated_resale_value: float
    high: float
    currency: str
    sample_size: int
    is_mock: bool
    condition: str
    multiplier_used: float


class AppraiseResponse(BaseModel):
    item: str
    labels: list[LabelOut]
    price_estimate: PriceOut
    # None when persistence is disabled or the save failed
    item_id: int | None = None
    estimate_id: int | None = None
    user_description: str | None = None
    # What eBay was actually searched for: "user_description" or "vision_label";
    # both None when the price came from the mock catalog
    search_term: str | None = None
    search_source: str | None = None


class HistoryEntry(BaseModel):
    estimate_id: int
    item_id: int
    category: str
    description: str | None
    user_description: str | None
    condition_label: str | None
    condition_multiplier: float | None
    market_median: float | None
    estimated_price: float | None
    source: str
    search_term: str | None
    search_source: str | None
    created_at: datetime
    sample_size: int
    low: float | None
    high: float | None


class EstimateOut(BaseModel):
    estimate_id: int
    market_median: float | None
    estimated_price: float | None
    source: str
    search_term: str | None
    search_source: str | None
    created_at: datetime


class ListingOut(BaseModel):
    listing_id: int
    ebay_listing_id: str | None
    sampled_price: float | None
    sampled_at: datetime


class ItemDetailOut(BaseModel):
    item_id: int
    category: str
    description: str | None
    user_description: str | None
    condition_label: str | None
    condition_multiplier: float | None
    created_at: datetime
    estimates: list[EstimateOut]
    listings: list[ListingOut]


class CategoryStat(BaseModel):
    category: str
    item_count: int
    avg_estimated_price: float | None
    avg_market_median: float | None


class CategoryPriceRange(BaseModel):
    category: str
    item_count: int
    listing_count: int
    low_price: float
    high_price: float
    price_range: float
    high_to_low_ratio: float | None
    range_rank: int


class CategoryVolume(BaseModel):
    category: str
    item_count: int


class OutcomeIn(BaseModel):
    final_price: Price | None = None
    sold_price: Price | None = None
    sold_at: datetime | None = None

    @field_validator("sold_at")
    @classmethod
    def _as_naive_utc(cls, value: datetime | None) -> datetime | None:
        # outcomes.sold_at is a DATETIME, which has no time zone
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @model_validator(mode="after")
    def _has_something_to_record(self) -> "OutcomeIn":
        if self.final_price is None and self.sold_price is None:
            raise ValueError("give final_price, sold_price, or both")
        if self.sold_at is not None and self.sold_price is None:
            raise ValueError("sold_at needs a sold_price")
        return self


class OutcomeOut(BaseModel):
    outcome_id: int
    estimate_id: int
    final_price: float
    sold_price: float | None
    sold_at: datetime | None
    created_at: datetime


class AccuracyStat(BaseModel):
    # None on the overall figures
    search_term: str | None
    n: int
    mape_pct: float
    median_ape_pct: float
    median_abs_error: float
    # Mean of estimate minus actual price; positive means the app prices too high
    bias: float
    is_below_min_n: bool


class AccuracyGroup(BaseModel):
    basis: str
    min_n: int
    overall: AccuracyStat | None
    by_search_term: list[AccuracyStat]


class AccuracyReport(BaseModel):
    # Estimates against sale prices: the accuracy figure
    accuracy: AccuracyGroup
    # Estimates against the price a volunteer set after seeing the estimate
    agreement_with_volunteer_price: AccuracyGroup


_vision = VisionClient()
_ebay = EbayClient()
_db_config = DbConfig.from_env()
if _db_config is None:
    logger.warning("MYSQL_DATABASE not set — appraisals will not be persisted")

app = FastAPI(title="Reappraise", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@contextmanager
def _db() -> Iterator[Connection]:
    """Connection for API endpoints; 503 if persistence isn't configured.

    A constraint violation is the request's fault, not an outage, so it maps to
    409; anything else (connection lost, server down) is a 503. Details stay in
    the server log either way.
    """
    if _db_config is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    try:
        with connect(_db_config) as conn:
            yield conn
    except HTTPException:
        raise
    except pymysql.err.IntegrityError as exc:
        logger.warning("Database constraint violation: %s", exc)
        raise HTTPException(
            status_code=409, detail="The request conflicts with data already saved"
        ) from exc
    except Exception as exc:
        logger.exception("Database error")
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


def _persist(
    category: str, description: str, user_description: str | None, estimate: PriceEstimate
) -> repository.SavedAppraisal | None:
    """Save an appraisal; returns its ids, or None if disabled or the save failed.

    A database outage shouldn't cost the user their price estimate, so failures
    are logged with full context and the appraisal is still returned.
    """
    if _db_config is None:
        return None
    try:
        with connect(_db_config) as conn:
            return repository.save_appraisal(
                conn,
                category=category,
                description=description,
                user_description=user_description,
                estimate=estimate,
            )
    except Exception:
        logger.exception("Failed to persist appraisal for %r", category)
        return None


@app.post("/appraise", response_model=AppraiseResponse)
async def appraise(
    file: UploadFile = File(...),
    condition: str = Query(default="fair"),
    # Optional brand/model/specifics from a volunteer; searched before Vision's labels
    user_description: str | None = Form(default=None, max_length=USER_DESCRIPTION_MAX),
) -> AppraiseResponse:
    if condition not in CONDITION_MULTIPLIERS:
        raise HTTPException(
            status_code=422,
            detail=f"condition must be one of: {', '.join(CONDITION_MULTIPLIERS)}",
        )

    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    manual_description = (user_description or "").strip() or None

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        labels = _vision.identify_item(image_bytes)
    except Exception as exc:
        # Upstream error text can contain request details; keep it in server logs
        logger.exception("Vision API call failed")
        raise HTTPException(status_code=502, detail="Vision API error") from exc

    if not labels:
        raise HTTPException(status_code=422, detail="No items detected in image")

    # Labels arrive ranked by confidence; the eBay client searches them one at a time
    label_names = [label.name for label in labels]
    description = " ".join(label_names[:3])
    # Name the item by its first specific label, so a generic top label like
    # "gadget" doesn't become the stored category; fall back if all are generic
    item_name = next(iter(specific_labels(label_names)), label_names[0])

    try:
        estimate = _ebay.search_labels(
            label_names, condition=condition, user_description=manual_description
        )
    except Exception as exc:
        logger.exception("eBay price lookup failed")
        raise HTTPException(status_code=502, detail="eBay API error") from exc

    if estimate.sample_size == 0:
        raise HTTPException(
            status_code=422,
            detail=f"No eBay listings found for '{item_name}' — try a clearer photo",
        )

    saved = _persist(
        category=item_name,
        description=description,
        user_description=manual_description,
        estimate=estimate,
    )

    return AppraiseResponse(
        item=item_name,
        labels=[LabelOut(name=lbl.name, confidence=lbl.confidence) for lbl in labels[:5]],
        price_estimate=PriceOut(
            low=estimate.low,
            market_median=estimate.market_median,
            estimated_resale_value=estimate.estimated_resale_value,
            high=estimate.high,
            currency=estimate.currency,
            sample_size=estimate.sample_size,
            is_mock=estimate.is_mock,
            condition=estimate.condition,
            multiplier_used=estimate.multiplier_used,
        ),
        item_id=saved.item_id if saved else None,
        estimate_id=saved.estimate_id if saved else None,
        user_description=manual_description,
        search_term=estimate.search_term,
        search_source=estimate.search_source,
    )


@app.get("/history", response_model=list[HistoryEntry])
def history(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    with _db() as conn:
        return repository.recent_estimates(conn, limit=limit)


@app.get("/history/{item_id}", response_model=ItemDetailOut)
def history_item(item_id: int) -> dict:
    with _db() as conn:
        detail = repository.get_item_detail(conn, item_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No item with id {item_id}")
    return {**detail.item, "estimates": detail.estimates, "listings": detail.listings}


@app.get("/analytics/categories", response_model=list[CategoryStat])
def analytics_categories() -> list[dict]:
    with _db() as conn:
        return repository.avg_price_by_category(conn)


@app.get("/analytics/price-range", response_model=list[CategoryPriceRange])
def analytics_price_range(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    with _db() as conn:
        return repository.price_range_by_category(conn, limit=limit)


@app.get("/analytics/top-category", response_model=list[CategoryVolume])
def analytics_top_category() -> list[dict]:
    with _db() as conn:
        return repository.top_category_by_volume(conn)


@app.post("/estimates/{estimate_id}/outcome", response_model=OutcomeOut)
def estimate_outcome(estimate_id: int, outcome: OutcomeIn, response: Response) -> dict:
    """Record the price a volunteer set for an estimate and, later, what it sold for.

    With a final_price this is a single upsert: 201 if it created the outcome,
    200 if one already existed. Without one (e.g. recording only the sale) it
    can only update, so an estimate with no outcome yet gets a 422.
    """
    fields = outcome.model_dump()
    with _db() as conn:
        if outcome.final_price is not None:
            recorded = repository.record_outcome(conn, estimate_id=estimate_id, **fields)
            if recorded is None:
                raise _no_estimate(estimate_id)
            if recorded.is_created:
                response.status_code = 201
            return recorded.row

        row = repository.update_outcome(conn, estimate_id=estimate_id, **fields)
        if row is None:
            if not repository.estimate_exists(conn, estimate_id):
                raise _no_estimate(estimate_id)
            raise HTTPException(
                status_code=422, detail="final_price is required for an estimate's first outcome"
            )
    return row


def _no_estimate(estimate_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"No estimate with id {estimate_id}")


@app.get("/analytics/accuracy", response_model=AccuracyReport)
def analytics_accuracy(min_n: int = Query(default=ACCURACY_MIN_N_DEFAULT, ge=1)) -> dict:
    with _db() as conn:
        rows = repository.accuracy_by_search_term(conn)
    flagged = [{**row, "is_below_min_n": row["n"] < min_n} for row in rows]
    return {
        "accuracy": _accuracy_group(flagged, ACCURACY_BASIS, min_n),
        "agreement_with_volunteer_price": _accuracy_group(flagged, AGREEMENT_BASIS, min_n),
    }


def _accuracy_group(rows: list[dict], basis: str, min_n: int) -> dict:
    matching = [row for row in rows if row["basis"] == basis]
    return {
        "basis": basis,
        "min_n": min_n,
        "overall": next((row for row in matching if row["search_term"] is None), None),
        "by_search_term": [row for row in matching if row["search_term"] is not None],
    }


# Must come after all @app.get / @app.post routes — Starlette's router matches
# in insertion order, so a Mount("/") placed earlier would intercept API paths.
# html=True makes StaticFiles serve index.html for bare "/" requests.
_FRONTEND = Path(__file__).parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
