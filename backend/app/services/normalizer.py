"""Text normalization helpers for medicine names and dosages."""
from __future__ import annotations
import re

# Tokens that identify form/route — stripped before matching
_FORM_TOKENS = re.compile(
    r"\b(tab(let)?s?|cap(sule)?s?|syp|syrup|inj(ection)?|oint(ment)?|"
    r"drops?|gel|cream|lotion|suspension|susp|soln?|solution|sachet|patch)\b",
    re.IGNORECASE,
)

# Dosage patterns like "500mg", "10 ml", "0.5%" — also bare integers NOT preceded by a hyphen
_DOSAGE_PATTERN = re.compile(
    r"\d+(\.\d+)?\s*(mg|mcg|µg|g|ml|l|iu|unit|units|%|w/v|w/w)|(?<![-\w])\d+(?!\w)",
    re.IGNORECASE,
)

# Punctuation except hyphens (kept for brand names like "Pan-40")
_PUNCTUATION = re.compile(r"[^\w\s\-]")

# Repeated whitespace
_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """
    Lowercase a medicine name and strip form tokens, dosage patterns, and
    extraneous punctuation.  Returns a clean string suitable for fuzzy matching.

    Examples
    --------
    >>> normalize_name("Tab. Dolo 650mg")
    'dolo'
    >>> normalize_name("Augmentin 625 Duo Tablet")
    'augmentin duo'
    >>> normalize_name("Pan-40 Cap")
    'pan-40'
    """
    text = name.lower()
    text = _FORM_TOKENS.sub(" ", text)
    text = _DOSAGE_PATTERN.sub(" ", text)
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def normalize_composition(composition: str) -> str:
    """
    Normalize a composition/salt string for fuzzy comparison.
    Lowercases, strips, removes parenthetical dosages, collapses whitespace.

    Examples
    --------
    >>> normalize_composition("Amoxycillin  (500mg) + Clavulanic Acid (125mg)")
    'amoxycillin + clavulanic acid'
    """
    text = composition.lower()
    # Remove bracketed dosage amounts
    text = re.sub(r"\(\s*[\d.\s]+\s*(mg|mcg|g|ml|iu|%)?\s*\)", " ", text, flags=re.IGNORECASE)
    text = _DOSAGE_PATTERN.sub(" ", text)
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def extract_strength(text: str) -> str | None:
    """
    Pull out the first dosage token from a string (e.g. '500mg', '10 ml').
    Returns None if nothing is found.
    """
    match = _DOSAGE_PATTERN.search(text)
    if match:
        return match.group(0).lower().replace(" ", "")
    return None
