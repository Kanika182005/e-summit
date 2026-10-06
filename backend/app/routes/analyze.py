"""POST /api/analyze, GET /api/search, GET /api/health routes."""

from __future__ import annotations
import logging

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, Request

from app.config import get_settings
from app.schemas import (
    AnalyzeResponse,
    HealthResponse,
    MedicineResult,
    Savings,
    SearchResult,
)
from app.services.preprocess import preprocess_image
from app.services.vision import extract_prescription
from app.services.matcher import match_brand, match_generic, search_all
from app.services.pricing import compute_savings

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
DISCLAIMER = (
    "AI-generated interpretation. Always confirm with your doctor or "
    "pharmacist before purchasing or substituting any medicine."
)


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness check — also reports how many records are loaded."""
    brands_df = getattr(request.app.state, "brands_df", None)
    generics_df = getattr(request.app.state, "generics_df", None)
    return HealthResponse(
        status="ok",
        brands_loaded=len(brands_df) if brands_df is not None else 0,
        generics_loaded=len(generics_df) if generics_df is not None else 0,
    )


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(file: UploadFile = File(...)) -> AnalyzeResponse:
    """
    Accept a prescription image, run Gemini Vision, fuzzy-match medicines,
    and return structured results with price savings.
    """
    settings = get_settings()

    # ── Validate upload ────────────────────────────────────────────────────
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{file.content_type}'. Upload JPG, PNG or WebP.",
        )

    image_bytes = await file.read()
    if len(image_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413, detail="File too large. Maximum size is 8 MB."
        )
    if len(image_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # ── Pre-process image ──────────────────────────────────────────────────
    try:
        image_bytes = preprocess_image(image_bytes)
    except Exception as exc:
        logger.exception("Image preprocessing failed")
        raise HTTPException(
            status_code=422, detail=f"Could not process image: {exc}"
        ) from exc

    # ── Gemini Vision ──────────────────────────────────────────────────────
    try:
        extraction = extract_prescription(image_bytes)
    except Exception as exc:
        logger.exception("Gemini extraction failed")
        raise HTTPException(
            status_code=502, detail=f"Vision service error: {exc}"
        ) from exc

    if not extraction.medicines:
        raise HTTPException(
            status_code=422,
            detail="No medicines could be found in the prescription image.",
        )

    # ── Matching pipeline per medicine ─────────────────────────────────────
    medicine_results: list[MedicineResult] = []
    total_saved = 0.0
    total_brand = 0.0

    for med in extraction.medicines:
        brand_match = match_brand(med.name)

        # Determine composition to use for generic matching
        composition = brand_match.composition or med.name
        preferred_strength = med.strength or brand_match.strength
        generic_match = match_generic(composition, preferred_strength)

        savings = compute_savings(brand_match.price, generic_match.mrp)

        needs_review = (
            med.confidence < 0.6
            or brand_match.score < settings.brand_match_cutoff
            or generic_match.score < settings.generic_match_cutoff
        )

        if savings.amount is not None and savings.amount > 0:
            total_saved += savings.amount
        if brand_match.price is not None:
            total_brand += brand_match.price

        medicine_results.append(
            MedicineResult(
                extracted=med,
                brand_match=brand_match,
                generic_match=generic_match,
                savings=savings,
                needs_review=needs_review,
            )
        )

    total_percent = (
        round((total_saved / total_brand) * 100, 1) if total_brand > 0 else None
    )
    total_savings = Savings(amount=round(total_saved, 2), percent=total_percent)

    return AnalyzeResponse(
        prescription={
            "patient_name": extraction.patient_name,
            "doctor_name": extraction.doctor_name,
            "date": extraction.date,
            "overall_legibility": extraction.overall_legibility,
            "notes": extraction.notes,
        },
        medicines=medicine_results,
        total_savings=total_savings,
        disclaimer=DISCLAIMER,
    )


@router.get("/search", response_model=SearchResult)
async def search(
    q: str = Query(..., min_length=2, description="Medicine name to search")
) -> SearchResult:
    """Fuzzy search across brand and generic databases for manual correction."""
    results = search_all(q)
    return SearchResult(**results)
