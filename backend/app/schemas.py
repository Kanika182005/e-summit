"""Pydantic v2 schemas for request/response contracts."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


# ── Gemini Vision output ──────────────────────────────────────────────────────

class ExtractedMedicine(BaseModel):
    raw_text: str
    name: str
    form: Optional[str] = None  # tablet|capsule|syrup|injection|ointment|drops|other
    strength: Optional[str] = None
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    frequency_plain: Optional[str] = None
    timing: Optional[str] = None
    duration: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class PrescriptionExtraction(BaseModel):
    patient_name: Optional[str] = None
    doctor_name: Optional[str] = None
    date: Optional[str] = None
    medicines: list[ExtractedMedicine] = []
    overall_legibility: str = "medium"  # high|medium|low
    notes: Optional[str] = None


# ── Matching output ───────────────────────────────────────────────────────────

class MatchCandidate(BaseModel):
    name: str
    score: float
    composition: Optional[str] = None
    strength: Optional[str] = None
    price: Optional[float] = None
    manufacturer: Optional[str] = None


class BrandMatch(BaseModel):
    name: Optional[str] = None
    composition: Optional[str] = None
    strength: Optional[str] = None
    price: Optional[float] = None
    score: float = 0.0
    alternatives: list[MatchCandidate] = []


class GenericMatch(BaseModel):
    product_code: Optional[str] = None
    name: Optional[str] = None
    strength: Optional[str] = None
    unit_size: Optional[str] = None
    mrp: Optional[float] = None
    score: float = 0.0
    strength_warning: bool = False
    alternatives: list[MatchCandidate] = []


class Savings(BaseModel):
    amount: Optional[float] = None
    percent: Optional[float] = None


class MedicineResult(BaseModel):
    extracted: ExtractedMedicine
    brand_match: BrandMatch = Field(default_factory=BrandMatch)
    generic_match: GenericMatch = Field(default_factory=GenericMatch)
    savings: Savings = Field(default_factory=Savings)
    needs_review: bool = False


# ── API response ──────────────────────────────────────────────────────────────

class AnalyzeResponse(BaseModel):
    prescription: dict
    medicines: list[MedicineResult]
    total_savings: Savings = Field(default_factory=Savings)
    disclaimer: str = (
        "AI-generated interpretation. Always confirm with your doctor or "
        "pharmacist before purchasing or substituting any medicine."
    )


class SearchResult(BaseModel):
    brand_candidates: list[MatchCandidate] = []
    generic_candidates: list[MatchCandidate] = []


class HealthResponse(BaseModel):
    status: str = "ok"
    brands_loaded: int = 0
    generics_loaded: int = 0
