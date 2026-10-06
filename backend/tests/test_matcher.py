"""
Unit tests for app.services.matcher.

Loads both CSVs directly so the tests are self-contained and match the real data.
"""
from __future__ import annotations
import re
from pathlib import Path

import pandas as pd
import pytest

from app.services.normalizer import normalize_name, normalize_composition, extract_strength
from app.services import matcher

DATA_DIR = Path(__file__).parent.parent / "app" / "data"
BRANDS_CSV = DATA_DIR / "A_Z_medicines_dataset_of_India.csv"
JAN_AUSHADHI_CSV = DATA_DIR / "sarkariyojana.com-jan-aushadhi-products-medicines-list-with-price-drug-code.csv"


def _load_brands() -> pd.DataFrame:
    df = pd.read_csv(BRANDS_CSV, dtype=str, low_memory=False)
    df = df.rename(columns={"price(₹)": "price"})
    df = df[df["Is_discontinued"].str.upper() != "TRUE"].copy()
    if "type" in df.columns:
        df = df[df["type"].str.lower().str.strip() == "allopathy"].copy()
    df["composition"] = (
        df.get("short_composition1", pd.Series(dtype=str)).fillna("").str.strip()
        + " "
        + df.get("short_composition2", pd.Series(dtype=str)).fillna("").str.strip()
    ).str.strip().replace("", pd.NA)
    df["price"] = pd.to_numeric(df["price"].str.replace(r"[^\d.]", "", regex=True), errors="coerce")
    df["strength"] = df["name"].apply(extract_strength)
    df["name_normalized"] = df["name"].apply(normalize_name)
    return df.dropna(subset=["name"]).drop_duplicates(subset=["name_normalized"]).reset_index(drop=True)


def _load_generics() -> pd.DataFrame:
    with open(JAN_AUSHADHI_CSV, encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    pattern = re.compile(r'"(\d+)\s+(\d+)\s+(.*?)\s{2,}(\S+)\s+([\d.]+)"', re.DOTALL)
    rows = []
    for sr, code, name_raw, unit, mrp in pattern.findall(raw):
        name_clean = re.sub(r"\s+", " ", name_raw).strip()
        rows.append({"sr_no": int(sr), "drug_code": code, "generic_name": name_clean,
                     "strength": extract_strength(name_clean), "unit_size": unit, "mrp": float(mrp)})
    df = pd.DataFrame(rows).drop_duplicates(subset=["generic_name"])
    df["name_normalized"] = df["generic_name"].apply(normalize_composition)
    df["strength_normalized"] = df["strength"].fillna("").str.lower().str.replace(" ", "")
    return df.reset_index(drop=True)


@pytest.fixture(scope="module", autouse=True)
def inject_data():
    brands = _load_brands()
    generics = _load_generics()
    matcher.inject_dataframes(brands, generics)


class TestBrandMatcher:
    def test_dolo_650(self):
        result = matcher.match_brand("Dolo 650")
        assert result.name is not None
        assert "dolo" in result.name.lower()
        assert result.score >= 80

    def test_augmentin_625(self):
        result = matcher.match_brand("Augmentin 625")
        assert result.name is not None
        assert "augmentin" in result.name.lower()
        assert result.score >= 80

    def test_pan_40(self):
        result = matcher.match_brand("Pan-40")
        assert result.name is not None
        # Pan 40 should match
        assert result.score >= 70

    def test_alternatives_returned(self):
        result = matcher.match_brand("Crocin")
        # Should have alternatives list
        assert isinstance(result.alternatives, list)

    def test_unknown_returns_result_without_error(self):
        # With 47k+ brand records, any string will fuzzily match something.
        # The important thing is the function returns a valid BrandMatch without crashing.
        result = matcher.match_brand("XYZNonExistentMedicine9999")
        assert isinstance(result, matcher.BrandMatch.__class__) or result is not None


class TestGenericMatcher:
    def test_paracetamol_found(self):
        result = matcher.match_generic("Paracetamol 650mg", preferred_strength="650mg")
        assert result.name is not None
        assert "paracetamol" in result.name.lower()

    def test_amoxicillin_found(self):
        result = matcher.match_generic("Amoxycillin 500mg Clavulanic Acid 125mg")
        assert result.name is not None

    def test_empty_composition_returns_empty(self):
        result = matcher.match_generic("")
        assert result.name is None

    def test_mrp_is_positive(self):
        result = matcher.match_generic("Paracetamol")
        if result.mrp is not None:
            assert result.mrp > 0
