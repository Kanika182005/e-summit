"""
FastAPI application entry point.

Startup sequence:
1. Load and clean brand CSV (A_Z_medicines_dataset_of_India.csv).
2. Parse and clean Jan Aushadhi CSV (sarkariyojana.com-...).
3. Inject DataFrames into the matcher module.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routes.analyze import router
from app.services import matcher
from app.services.normalizer import normalize_name, normalize_composition, extract_strength

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
BRANDS_CSV = DATA_DIR / "A_Z_medicines_dataset_of_India.csv"
JAN_AUSHADHI_CSV = DATA_DIR / "sarkariyojana.com-jan-aushadhi-products-medicines-list-with-price-drug-code.csv"


# ── Data loading helpers ──────────────────────────────────────────────────────

def _load_brands() -> pd.DataFrame:
    """
    Load the A-Z medicines dataset.
    Expected columns: id, name, price(₹), Is_discontinued, manufacturer_name,
                      type, pack_size_label, short_composition1, short_composition2
    """
    logger.info("Loading brands from %s", BRANDS_CSV)
    df = pd.read_csv(BRANDS_CSV, dtype=str, low_memory=False)

    # Rename for consistency
    df = df.rename(columns={"price(₹)": "price"})

    # Keep only active allopathy drugs
    df = df[df["Is_discontinued"].str.upper() != "TRUE"].copy()
    if "type" in df.columns:
        df = df[df["type"].str.lower().str.strip() == "allopathy"].copy()

    # Build unified composition column
    df["composition"] = (
        df.get("short_composition1", pd.Series(dtype=str)).fillna("").str.strip()
        + " "
        + df.get("short_composition2", pd.Series(dtype=str)).fillna("").str.strip()
    ).str.strip()
    df["composition"] = df["composition"].replace("", pd.NA)

    # Parse price (strip currency symbols / commas)
    df["price"] = pd.to_numeric(
        df["price"].str.replace(r"[^\d.]", "", regex=True), errors="coerce"
    )

    # Extract strength from name
    df["strength"] = df["name"].apply(extract_strength)

    # Normalised lookup columns
    df["name_normalized"] = df["name"].apply(normalize_name)

    # Drop rows with no usable name
    df = df.dropna(subset=["name"]).drop_duplicates(subset=["name_normalized"])

    logger.info("Brands loaded: %d rows", len(df))
    return df.reset_index(drop=True)


def _load_jan_aushadhi() -> pd.DataFrame:
    """
    Parse the sarkariyojana Jan Aushadhi CSV, which has a non-standard format
    where each record is a multi-line quoted cell:

        "1   2   Medicine Name\\n   unit_size   mrp"
    """
    logger.info("Loading Jan Aushadhi from %s", JAN_AUSHADHI_CSV)
    with open(JAN_AUSHADHI_CSV, encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    pattern = re.compile(
        r'"(\d+)\s+(\d+)\s+(.*?)\s{2,}(\S+)\s+([\d.]+)"',
        re.DOTALL,
    )
    matches = pattern.findall(raw)
    logger.info("Jan Aushadhi raw records parsed: %d", len(matches))

    rows = []
    for sr, drug_code, name_raw, unit_size, mrp_str in matches:
        # Clean multi-line artefacts in the name
        name_clean = re.sub(r"\s+", " ", name_raw).strip()
        # Extract strength from the name
        strength = extract_strength(name_clean)
        rows.append(
            {
                "sr_no": int(sr),
                "drug_code": drug_code,
                "generic_name": name_clean,
                "strength": strength,
                "unit_size": unit_size,
                "mrp": float(mrp_str),
            }
        )

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["generic_name"])

    # Normalised lookup column (used for fuzzy matching against composition)
    df["name_normalized"] = df["generic_name"].apply(normalize_composition)
    # Also keep a strength_normalized for preference matching
    df["strength_normalized"] = df["strength"].fillna("").str.lower().str.replace(" ", "")

    logger.info("Jan Aushadhi loaded: %d rows", len(df))
    return df.reset_index(drop=True)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="DawaRx API",
        description="AI Prescription Decipherer & Generic Finder",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api")

    @app.on_event("startup")
    async def startup() -> None:
        brands_df = _load_brands()
        generics_df = _load_jan_aushadhi()
        # Inject into matcher
        matcher.inject_dataframes(brands_df, generics_df)
        # Also store on app.state for health route
        app.state.brands_df = brands_df
        app.state.generics_df = generics_df
        logger.info("🚀 DawaRx API ready")

    return app


app = create_app()
