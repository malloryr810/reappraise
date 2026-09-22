from dotenv import load_dotenv

load_dotenv()  # must run before EbayClient reads env vars

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymysql.connections import Connection

import repository
from db import DbConfig, connect
from ebay import CONDITION_MULTIPLIERS, EbayClient, PriceEstimate, specific_labels
from vision import VisionClient

logger = logging.getLogger("reappraise")

# Matches items.user_description VARCHAR(255); longer input is rejected, not trimmed
USER_DESCRIPTION_MAX = 255


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
    """Connection for read endpoints; 503 if persistence isn't configured."""
    if _db_config is None:
        raise HTTPException(status_code=503, detail="Persistence is not configured")
    try:
        with connect(_db_config) as conn:
            yield conn
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Database error")
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


def _persist(
    category: str, description: str, user_description: str | None, estimate: PriceEstimate
) -> int | None:
    """Save an appraisal; returns item_id, or None if disabled or the save failed.

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

    item_id = _persist(
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
        item_id=item_id,
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


# Must come after all @app.get / @app.post routes — Starlette's router matches
# in insertion order, so a Mount("/") placed earlier would intercept API paths.
# html=True makes StaticFiles serve index.html for bare "/" requests.
_FRONTEND = Path(__file__).parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
