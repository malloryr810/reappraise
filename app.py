from dotenv import load_dotenv

load_dotenv()  # must run before EbayClient reads env vars

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ebay import EbayClient
from vision import VisionClient


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


_vision = VisionClient()
_ebay = EbayClient()

app = FastAPI(title="Reappraise", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/appraise", response_model=AppraiseResponse)
async def appraise(
    file: UploadFile = File(...),
    condition: str = Query(default="fair"),
) -> AppraiseResponse:
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        labels = _vision.identify_item(image_bytes)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Vision API error: {exc}") from exc

    if not labels:
        raise HTTPException(status_code=422, detail="No items detected in image")

    query = " ".join(label.name for label in labels[:3])

    try:
        estimate = _ebay.search_prices(query, condition=condition)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"eBay API error: {exc}") from exc

    if estimate.sample_size == 0:
        raise HTTPException(
            status_code=422,
            detail=f"No eBay listings found for '{labels[0].name}' — try a clearer photo",
        )

    return AppraiseResponse(
        item=labels[0].name,
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
    )


# Must come after all @app.get / @app.post routes — Starlette's router matches
# in insertion order, so a Mount("/") placed earlier would intercept API paths.
# html=True makes StaticFiles serve index.html for bare "/" requests.
_FRONTEND = Path(__file__).parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
