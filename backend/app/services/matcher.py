"""
Fuzzy matching pipeline: brand name → composition → Jan Aushadhi generic.

Uses RapidFuzz WRatio / token_set_ratio.  Both DataFrames are injected at
startup from main.py after Pandas loading.
"""
from __future__ import annotations
import logging
from typing import Optional

import pandas as pd
from rapidfuzz import fuzz, process

from app.services.normalizer import normalize_name, normalize_composition, extract_strength
from app.schemas import BrandMatch, GenericMatch, MatchCandidate
from app.config import get_settings

logger = logging.getLogger(__name__)

# Module-level DataFrames — populated by inject_dataframes() at startup
_brands_df: Optional[pd.DataFrame] = None
_generics_df: Optional[pd.DataFrame] = None


def _safe(value) -> Optional[str]:
    """Convert pandas NaN / None / non-string to Optional[str]."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value) if not isinstance(value, str) else value


def inject_dataframes(brands: pd.DataFrame, generics: pd.DataFrame) -> None:
    """Called once at startup to inject the cleaned DataFrames."""
    global _brands_df, _generics_df
    _brands_df = brands
    _generics_df = generics
    logger.info("Matcher loaded: %d brands, %d generics", len(brands), len(generics))


# ── Brand matching ────────────────────────────────────────────────────────────

def match_brand(name: str, top_n: int = 3) -> BrandMatch:
    """
    Fuzzy-match a medicine name against the brand database.

    Returns a BrandMatch with the best hit and up to top_n alternatives.
    """
    if _brands_df is None or _brands_df.empty:
        return BrandMatch()

    settings = get_settings()
    query = normalize_name(name)
    choices = _brands_df["name_normalized"].tolist()

    results = process.extract(
        query,
        choices,
        scorer=fuzz.WRatio,
        limit=top_n,
        score_cutoff=0,
    )

    if not results:
        return BrandMatch()

    best_text, best_score, best_idx = results[0]
    threshold = settings.brand_match_cutoff

    # Build candidate list (all top_n)
    alternatives: list[MatchCandidate] = []
    for text, score, idx in results[1:]:
        row = _brands_df.iloc[idx]
        alternatives.append(
            MatchCandidate(
                name=row["name"],
                score=round(score, 1),
                composition=_safe(row.get("composition")),
                strength=_safe(row.get("strength")),
                price=row.get("price") if pd.notna(row.get("price")) else None,
                manufacturer=_safe(row.get("manufacturer_name")),
            )
        )

    if best_score < threshold:
        logger.debug("Brand match below threshold: '%s' → %.1f", query, best_score)
        return BrandMatch(score=round(best_score, 1), alternatives=alternatives)

    best_row = _brands_df.iloc[best_idx]
    return BrandMatch(
        name=best_row["name"],
        composition=_safe(best_row.get("composition")),
        strength=_safe(best_row.get("strength")),
        price=best_row.get("price") if pd.notna(best_row.get("price")) else None,
        score=round(best_score, 1),
        alternatives=alternatives,
    )


# ── Generic (Jan Aushadhi) matching ──────────────────────────────────────────

def match_generic(
    composition: Optional[str],
    preferred_strength: Optional[str] = None,
    top_n: int = 3,
) -> GenericMatch:
    """
    Fuzzy-match a composition string against the Jan Aushadhi generic list.

    Prefers entries whose strength matches preferred_strength; otherwise
    returns the best compositional match and sets strength_warning=True.
    """
    if _generics_df is None or _generics_df.empty or not composition:
        return GenericMatch()

    settings = get_settings()
    query = normalize_composition(composition)
    choices = _generics_df["name_normalized"].tolist()

    results = process.extract(
        query,
        choices,
        scorer=fuzz.token_set_ratio,
        limit=top_n * 2,  # fetch more to filter by strength
        score_cutoff=0,
    )

    if not results:
        return GenericMatch()

    threshold = settings.generic_match_cutoff

    # Filter to above-threshold results
    above = [(t, s, i) for t, s, i in results if s >= threshold]
    candidates = above if above else results[:top_n]

    # Try to find a strength match
    best_text, best_score, best_idx = candidates[0]
    strength_warning = False

    if preferred_strength:
        pref_norm = preferred_strength.lower().replace(" ", "")
        strength_matches = [
            (t, s, i)
            for t, s, i in candidates
            if _generics_df.iloc[i].get("strength_normalized", "") == pref_norm
        ]
        if strength_matches:
            best_text, best_score, best_idx = strength_matches[0]
        else:
            strength_warning = True
            logger.debug(
                "No strength match for '%s' @ %s; using best compositional hit",
                composition,
                preferred_strength,
            )

    best_row = _generics_df.iloc[best_idx]

    # Build alternatives list (exclude best)
    alternatives: list[MatchCandidate] = []
    seen = {best_idx}
    for text, score, idx in candidates:
        if idx in seen or len(alternatives) >= top_n - 1:
            continue
        seen.add(idx)
        row = _generics_df.iloc[idx]
        alternatives.append(
            MatchCandidate(
                name=row["generic_name"],
                score=round(score, 1),
                strength=_safe(row.get("strength")),
                price=row.get("mrp") if pd.notna(row.get("mrp")) else None,
            )
        )

    if best_score < threshold:
        return GenericMatch(score=round(best_score, 1), strength_warning=strength_warning, alternatives=alternatives)

    return GenericMatch(
        product_code=str(best_row.get("drug_code", "")),
        name=best_row["generic_name"],
        strength=_safe(best_row.get("strength")),
        unit_size=_safe(best_row.get("unit_size")),
        mrp=best_row.get("mrp") if pd.notna(best_row.get("mrp")) else None,
        score=round(best_score, 1),
        strength_warning=strength_warning,
        alternatives=alternatives,
    )


# ── Search (manual correction) ────────────────────────────────────────────────

def search_all(query: str, top_n: int = 5) -> dict:
    """
    Search both brand and generic DBs with a free-text query.
    Used by GET /api/search.
    """
    brand_candidates: list[MatchCandidate] = []
    generic_candidates: list[MatchCandidate] = []

    if _brands_df is not None and not _brands_df.empty:
        q = normalize_name(query)
        results = process.extract(q, _brands_df["name_normalized"].tolist(), scorer=fuzz.WRatio, limit=top_n)
        for _, score, idx in results:
            row = _brands_df.iloc[idx]
            brand_candidates.append(
                MatchCandidate(
                    name=row["name"],
                    score=round(score, 1),
                    composition=_safe(row.get("composition")),
                    strength=_safe(row.get("strength")),
                    price=row.get("price") if pd.notna(row.get("price")) else None,
                )
            )

    if _generics_df is not None and not _generics_df.empty:
        q = normalize_name(query)
        results = process.extract(q, _generics_df["name_normalized"].tolist(), scorer=fuzz.token_set_ratio, limit=top_n)
        for _, score, idx in results:
            row = _generics_df.iloc[idx]
            generic_candidates.append(
                MatchCandidate(
                    name=row["generic_name"],
                    score=round(score, 1),
                    strength=_safe(row.get("strength")),
                    price=row.get("mrp") if pd.notna(row.get("mrp")) else None,
                )
            )

    return {"brand_candidates": brand_candidates, "generic_candidates": generic_candidates}
