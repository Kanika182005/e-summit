"""Savings calculation helpers."""
from __future__ import annotations
from typing import Optional
from app.schemas import Savings


def compute_savings(brand_price: Optional[float], generic_mrp: Optional[float]) -> Savings:
    """
    Compute rupee amount saved and percentage saved.

    Returns a Savings object with None values if either price is missing.
    """
    if brand_price is None or generic_mrp is None:
        return Savings()
    amount = round(brand_price - generic_mrp, 2)
    percent = round((amount / brand_price) * 100, 1) if brand_price > 0 else None
    return Savings(amount=amount, percent=percent)
