"""
Gemini Vision service: sends a prescription image to Gemini and returns
structured JSON matching PrescriptionExtraction schema.

Features:
- Structured output via response_mime_type + response_schema.
- Retry logic (max 2 retries).
- Graceful mock fallback when GEMINI_API_KEY is not set.
"""
from __future__ import annotations
import base64
import json
import logging
import time
from typing import Optional

from app.config import get_settings
from app.schemas import PrescriptionExtraction, ExtractedMedicine

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert medical transcriptionist specialising in reading
messy, handwritten Indian doctor prescriptions. Your job is to extract all medicines
and related information as accurately as possible.

RULES:
1. Interpret common Indian medical abbreviations: OD (once daily), BD (twice daily),
   TDS (three times daily), QID (four times daily), HS (at bedtime), SOS (as needed),
   AC (before food), PC (after food), Tab (tablet), Cap (capsule), Syp (syrup),
   Inj (injection). Also interpret 1-0-1 notation (morning-afternoon-night).
2. Never invent medicine names. If unsure, keep raw_text and set confidence low.
3. Set confidence between 0.0 (completely unreadable) and 1.0 (perfectly clear).
4. Return null for any field you cannot read.
5. overall_legibility: "high" if most text is clear, "medium" if partially legible,
   "low" if mostly illegible.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": {"type": "string", "nullable": True},
        "doctor_name": {"type": "string", "nullable": True},
        "date": {"type": "string", "nullable": True},
        "medicines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_text": {"type": "string"},
                    "name": {"type": "string"},
                    "form": {"type": "string", "nullable": True},
                    "strength": {"type": "string", "nullable": True},
                    "dosage": {"type": "string", "nullable": True},
                    "frequency": {"type": "string", "nullable": True},
                    "frequency_plain": {"type": "string", "nullable": True},
                    "timing": {"type": "string", "nullable": True},
                    "duration": {"type": "string", "nullable": True},
                    "confidence": {"type": "number"},
                },
                "required": ["raw_text", "name", "confidence"],
            },
        },
        "overall_legibility": {"type": "string"},
        "notes": {"type": "string", "nullable": True},
    },
    "required": ["medicines", "overall_legibility"],
}

MOCK_RESPONSE = PrescriptionExtraction(
    patient_name="Demo Patient",
    doctor_name="Dr. Demo",
    date="01/01/2025",
    medicines=[
        ExtractedMedicine(
            raw_text="Tab Dolo 650 mg 1-0-1 × 5 days PC",
            name="Dolo 650",
            form="tablet",
            strength="650mg",
            dosage="1 tablet",
            frequency="1-0-1",
            frequency_plain="Twice a day (morning and night)",
            timing="after food",
            duration="5 days",
            confidence=0.95,
        ),
        ExtractedMedicine(
            raw_text="Tab Pan 40 OD AC",
            name="Pan 40",
            form="tablet",
            strength="40mg",
            dosage="1 tablet",
            frequency="OD",
            frequency_plain="Once a day",
            timing="before food",
            duration=None,
            confidence=0.90,
        ),
        ExtractedMedicine(
            raw_text="Tab Augmentin 625 BD × 7 days PC",
            name="Augmentin 625",
            form="tablet",
            strength="625mg",
            dosage="1 tablet",
            frequency="BD",
            frequency_plain="Twice a day",
            timing="after food",
            duration="7 days",
            confidence=0.88,
        ),
    ],
    overall_legibility="high",
    notes="Mock response — no API key set.",
)


def _call_gemini(image_bytes: bytes) -> PrescriptionExtraction:
    """Make the actual Gemini API call with retry logic."""
    try:
        import google.genai as genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError as exc:
        raise RuntimeError("google-genai package not installed") from exc

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    image_b64 = base64.b64encode(image_bytes).decode()

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=[
                    genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part(text=SYSTEM_PROMPT),
                            genai_types.Part(
                                inline_data=genai_types.Blob(
                                    mime_type="image/jpeg",
                                    data=image_b64,
                                )
                            ),
                            genai_types.Part(
                                text="Extract all medicines and prescription info from this image."
                            ),
                        ],
                    )
                ],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0.1,
                ),
            )
            raw = response.text
            data = json.loads(raw)
            return PrescriptionExtraction(**data)

        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(1.5 ** attempt)
            else:
                raise

    raise RuntimeError("All Gemini retries exhausted")


def extract_prescription(image_bytes: bytes) -> PrescriptionExtraction:
    """
    Main entry point.  Returns a PrescriptionExtraction.
    Falls back to MOCK_RESPONSE if no API key is configured.
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY not set — returning mock response")
        return MOCK_RESPONSE
    return _call_gemini(image_bytes)
