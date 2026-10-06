"""
matcher.py - match prescribed medicines to the Jan Aushadhi product list.

What this file does
-------------------
1. Reads the official Jan Aushadhi product-list PDF (once, using word x-positions to split its
   Sr.No | Drug Code | Generic Name | Unit Size | MRP columns) and caches it as JSON.
2. Splits every product name into: active ingredients, strengths, dosage form
   and release type (e.g. "prolonged release").
3. Matches a prescribed medicine in three levels:
      Level 1  exact name       (name + strength + form)
      Level 2  composition      (ALL ingredients + strengths + form)
      Level 3  controlled fuzzy (typo correction of ingredient names only)
4. Returns a JSON-serialisable dict with price, confidence and an explanation.

SAFETY RULES (medical project: a wrong match is worse than no match)
    * Different ingredient, strength, combination or incompatible form -> reject.
    * Missing strength -> "strength_missing", never guessed.
    * Missing composition for a brand name -> "composition_required", never guessed.
    * Anything uncertain -> manual_verification_required = True.

Nothing in this file contains medicine or price data. All of it comes from the PDF.

Install:   pip install pdfplumber rapidfuzz
Optional:  pip install pytesseract pdf2image      (only for scanned PDFs)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from rapidfuzz.distance import Levenshtein
except ImportError:  # pure-Python fallback so typo correction still works
    class Levenshtein:  # type: ignore[no-redef]
        @staticmethod
        def distance(a: str, b: str) -> int:
            if a == b:
                return 0
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                cur = [i]
                for j, cb in enumerate(b, 1):
                    cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
                prev = cur
            return prev[-1]

log = logging.getLogger("matcher")

PARSER_VERSION = 5  # bump when parsing logic changes -> caches rebuild automatically
DISCLAIMER = ("Potential Jan Aushadhi match found. Confirm substitution with a "
              "doctor/pharmacist. Do not substitute on your own.")

# --- scoring constants (all in one place so the scoring is transparent) -----
BASE_EXACT_NAME = 0.98          # same name + strength, form/release verified
BASE_COMPOSITION = 0.96         # same ingredients + strengths
FUZZY_PENALTY = 0.06            # per typo-corrected ingredient
FUZZY_MAX = 0.88                # a fuzzy match can never exceed this
DED_FORM_MISSING = 0.12         # prescription gave no dosage form
DED_FORM_PARTIAL = 0.12         # e.g. syrup vs suspension
DED_SALT_RX_MISSING = 0.03      # Rx says "Metformin", list says "Metformin Hydrochloride"
DED_SALT_JA_MISSING = 0.10      # Rx names a salt the list does not state (hard flag)
DED_PER_UNVERIFIED = 0.15       # "125 mg" vs "125 mg per 5 ml"
DED_RELEASE = {"delayed": 0.06, "extended": 0.15, "dispersible": 0.10,
               "chewable": 0.10, "odt": 0.10}   # list product has release type Rx lacks
MIN_CONFIDENCE = 0.70           # below this -> no match
MANUAL_BELOW = 0.88             # below this -> manual verification


# =============================================================================
# 1. TEXT NORMALISATION
# =============================================================================
# Spelling / naming variants ONLY (not medicine data). Extend as you meet new ones.
SPELLING_FIXES = {
    "potassium clavulanate": "clavulanic acid", "clavulanate potassium": "clavulanic acid",
    "clavulanate": "clavulanic acid",
    "sulphamethoxazole": "sulfamethoxazole", "sulphate": "sulfate", "sulphacetamide": "sulfacetamide",
    "sulphadiazine": "sulfadiazine", "sulphasalazine": "sulfasalazine", "picosulphate": "picosulfate",
    "paediatric": "pediatric", "amoxycillin": "amoxicillin", "cetrizine": "cetirizine",
    "levocetrizine": "levocetirizine", "frusemide": "furosemide", "lignocaine": "lidocaine",
    "acetaminophen": "paracetamol", "aciclovir": "acyclovir", "cefalexin": "cephalexin",
    "levo-thyroxine": "levothyroxine", "thyroxine": "levothyroxine", "cyclosporin": "cyclosporine",
    "nicoumalone": "acenocoumarol", "albuterol": "salbutamol", "glyburide": "glibenclamide",
    "tazobactum": "tazobactam", "methylcobalamin": "mecobalamin", "dothiepin": "dosulepin",
    "dosulepine": "dosulepin", "oxelate": "oxalate", "nortryptyline": "nortriptyline",
    "trihexiphenidyl": "trihexyphenidyl", "dicycloverine": "dicyclomine", "guaiphenesin": "guaifenesin",
    "vitamin d3": "cholecalciferol", "vitamin b12": "cyanocobalamin", "cyanocobalamine": "cyanocobalamin",
    "tartarate": "tartrate", "besylate": "besilate", "mesylate": "mesilate", "dihydrochloride": "hydrochloride",
    "hcl": "hydrochloride", "magnessium": "magnesium", "tabelts": "tablets", "capulses": "capsules",
}
_SPELL_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(SPELLING_FIXES, key=len, reverse=True)) + r")\b")

# Words that are salts/esters of an active moiety ("diclofenac SODIUM", "metformin HYDROCHLORIDE").
SALT_WORDS = {
    "hydrochloride", "hydrobromide", "sodium", "potassium", "sulfate", "maleate", "besilate", "succinate",
    "tartrate", "phosphate", "mesilate", "citrate", "acetate", "fumarate", "oxalate", "medoxomil", "axetil",
    "proxetil", "propionate", "dipropionate", "valerate", "furoate", "nitrate", "bromide", "tromethamine",
    "carbonate", "gluconate", "diethylamine", "etabonate", "disoproxil", "alafenamide", "pamoate", "stearate",
    "estolate", "decanoate", "erbumine", "magnesium", "calcium", "zinc", "besylate",
}
IGNORABLE_SALT_WORDS = {"trihydrate", "monohydrate", "dihydrate", "hydrate", "anhydrous", "hemihydrate"}
# If an ingredient STARTS with one of these, the whole phrase is the ingredient ("sodium valproate").
MINERAL_FIRST = {"calcium", "magnesium", "zinc", "sodium", "potassium", "lithium", "ferrous", "ferric", "iron",
                 "silver", "aluminium", "aluminum", "copper", "selenium", "manganese", "chromium",
                 "strontium", "dried", "carbonyl"}


def clean_text(s: Any) -> str:
    """Lower-case, tidy punctuation/whitespace. '15,00,000' -> '1500000'."""
    s = str(s or "")
    s = s.replace("\u00a0", " ").replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    return re.sub(r"\s+", " ", s.lower()).strip()


def apply_spelling(s: str) -> str:
    return _SPELL_RE.sub(lambda m: SPELLING_FIXES[m.group(1)], s)


# ----- strengths -------------------------------------------------------------
STRENGTH_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>(?:mcg|µg|μg|ug|mg|gms?|g|iu|units?|ml|million|billion)(?![a-z])|%)"
    r"\s*(?P<basis>w\s*/\s*[wv]|v\s*/\s*[vw])?"
    r"(?:\s*(?:per|/)\s*(?P<pv>\d+(?:\.\d+)?)?\s*(?P<pu>ml|gm|g|l)(?![a-z]))?"
)
_MASS = {"mcg": 0.001, "µg": 0.001, "μg": 0.001, "ug": 0.001, "mg": 1.0, "g": 1000.0, "gm": 1000.0, "gms": 1000.0}


def _num(x: float) -> str:
    """1500000.0 -> '1500000', 0.25 -> '0.25' (never '1.5e+06')."""
    return f"{x:.6f}".rstrip("0").rstrip(".")


def strength_from_match(m: "re.Match") -> Dict[str, Any]:
    """Turn a regex match into a comparable strength dict (mass -> mg, g -> 1000 mg, ...)."""
    val, unit = float(m.group("val")), m.group("unit")
    if unit in _MASS:
        u, amt = "mg", val * _MASS[unit]
    elif unit in ("iu", "unit", "units"):
        u, amt = "iu", val
    elif unit in ("million", "billion"):
        u, amt = "count", val * (1e6 if unit == "million" else 1e9)
    else:                                   # '%' or 'ml'
        u, amt = unit, val
    per = per_unit = None
    if m.group("pu"):
        per = float(m.group("pv")) if m.group("pv") else 1.0
        per_unit = m.group("pu")
        if per_unit == "l":
            per, per_unit = per * 1000, "ml"
        if per_unit == "gm":
            per_unit = "g"
    basis = re.sub(r"\s", "", m.group("basis")) if m.group("basis") else None
    text = f"{_num(val)} {unit}" + (f" {basis}" if basis else "") + (f" per {_num(per)} {per_unit}" if per else "")
    return {"amount": amt, "unit": u, "per": per, "per_unit": per_unit, "basis": basis, "text": text,
            "key_text": re.sub(r"\s", "", text)}


def _close(x: float, y: float) -> bool:
    return abs(x - y) <= 1e-6 * max(1.0, abs(x), abs(y))


def compare_strength(rx: Optional[dict], ja: Optional[dict]) -> str:
    """Return 'equal', 'per_unverified', 'different' or 'missing'."""
    if rx is None or ja is None:
        return "missing"
    if rx["unit"] != ja["unit"]:
        return "different"
    if rx["unit"] == "%":
        if not _close(rx["amount"], ja["amount"]):
            return "different"
        if rx["basis"] and ja["basis"] and rx["basis"] != ja["basis"]:
            return "different"
        return "equal"
    if rx["per"] is None and ja["per"] is None:
        return "equal" if _close(rx["amount"], ja["amount"]) else "different"
    if rx["per"] is not None and ja["per"] is not None:
        if rx["per_unit"] != ja["per_unit"]:
            return "different"
        return "equal" if _close(rx["amount"] / rx["per"], ja["amount"] / ja["per"]) else "different"
    # one side has "per X ml", the other doesn't: only the raw amount can be compared
    return "per_unverified" if _close(rx["amount"], ja["amount"]) else "different"


def _strength_key(s: Optional[dict]) -> str:
    if s is None:
        return "nostrength"
    v = s["amount"] / s["per"] if s["per"] else s["amount"]
    return f"{s['unit']}|{v:.6f}|{s['per_unit'] if s['per'] else ''}"


# ----- dosage form & release type -------------------------------------------
FORM_PATTERNS: List[Tuple[str, str]] = [   # order matters: first match wins
    ("eye_ear_drops", r"eye\s*/\s*ear\s*drops?"),
    ("eye_ointment", r"eye\s+ointment"),
    ("eye_drops", r"eye\s*drops?|ophthalmic\s+(?:solution|suspension)"),
    ("ear_drops", r"\bear\s*drops?"),
    ("nasal_drops", r"nasal\s*drops?"),
    ("kit", r"combi\s*pack|combi\s*kit|combo\s*pack|\bkit\b"),
    ("injection", r"injections?|\binj\b"),
    ("infusion", r"infusion"),
    ("respules", r"respules?|respirator\s+(?:solution|suspension)|nebuli[sz]er\s+suspension"),
    ("inhaler", r"inhaler|inhalation|rotacaps?|\bmdi\b"),
    ("tablet", r"tablets?|\btabs?\b|caplets?"),
    ("capsule", r"capsules?|\bcaps?\b|soft\s*gel\w*|softgel"),
    ("suspension", r"suspension|dry\s+syrup|\bsusp\b"),
    ("syrup", r"syrup|\bsyp\b|elixir|expectorant"),
    ("solution", r"solution|\bsoln?\b"),
    ("drops", r"\bdrops?\b"),
    ("cream", r"cream"), ("ointment", r"ointment|\boint\b"), ("gel", r"\bgel\b"), ("lotion", r"lotion"),
    ("shampoo", r"shampoo"), ("spray", r"spray"), ("mouthwash", r"mouth\s*wash|gargle|mouth\s*paint"),
    ("patch", r"patch"), ("strip", r"\bstrips?\b"), ("suppository", r"suppositor\w+"),
    ("powder", r"powder|granules|sachet"),
]
_FORM_RES = [(name, re.compile(p)) for name, p in FORM_PATTERNS]

MODIFIER_PATTERNS: List[Tuple[str, str]] = [
    ("extended", r"(?:prolonged|sustained|extended|modified|controlled)[\s\-]*releases?|\b(?:sr|er|xr|xl|mr|cr)\b|\bretard\b"),
    ("delayed", r"gastro[\s\-]*resist\w*|enteric[\s\-]*coated|delayed[\s\-]*releases?"),
    ("dispersible", r"\bdispersible\b"),
    ("chewable", r"\bchewable\b"),
    ("odt", r"orally[\s\-]*disintegrating|oral[\s\-]*disintegrating|mouth[\s\-]*dissolving|orodispersible|\bodt\b"),
]
_MOD_RES = [(name, re.compile(p)) for name, p in MODIFIER_PATTERNS]
_MOD_ALL = re.compile("|".join(p for _, p in MODIFIER_PATTERNS))
# words removed before reading ingredients out of a product name
_FORM_WORDS = re.compile(
    r"\b(?:tablets?|tabs?|capsules?|caps?|soft\s*gel\w*|softgel|injections?|inj|infusion|intravenous|for|oral|"
    r"suspension|syrup|solution|drops?|eye|ear|nasal|ophthalmic|cream|ointment|gel|lotion|shampoo|spray|powder|"
    r"dusting|inhaler|inhalation|rotacaps?|respules?|ip|i\.p\.?|bp|usp|nfi|wfi|vial|pediatric|flavou?r|"
    r"expectorant|mouthwash|mouth|paint|gargle|sachet|granules|strips?|patch|transdermal|dry|with)\b")

DROPS_FORMS = {"eye_drops", "ear_drops", "nasal_drops", "eye_ear_drops"}


def detect_form(text: str) -> Optional[str]:
    t = clean_text(text).replace("_", " ")
    for name, rx in _FORM_RES:
        if rx.search(t):
            return name
    return None


def detect_modifiers(text: str) -> set:
    t = clean_text(text)
    return {name for name, rx in _MOD_RES if rx.search(t)}


def form_compat(rx_form: str, ja_form: str) -> str:
    """'same', 'partial' (probably OK but must be checked) or 'different'."""
    if rx_form == ja_form:
        return "same"
    if {rx_form, ja_form} <= {"syrup", "suspension", "solution"}:
        return "partial"
    if {rx_form, ja_form} <= {"injection", "infusion"}:
        return "partial"
    if "drops" in (rx_form, ja_form) and ({rx_form, ja_form} - {"drops"}) <= DROPS_FORMS:
        return "partial"
    return "different"


def pretty_form(f: Optional[str]) -> Optional[str]:
    return f.replace("_", " ").title() if f else None


# =============================================================================
# 2. PRODUCT-NAME ANALYSIS  ("Aceclofenac 100mg and Paracetamol 325mg Tablets")
# =============================================================================
def split_salt(tokens: List[str]) -> Tuple[str, str]:
    """['metformin','hydrochloride'] -> ('metformin', 'hydrochloride')."""
    if len(tokens) < 2 or tokens[0] in MINERAL_FIRST:
        return " ".join(tokens), ""
    salt: List[str] = []
    rest = list(tokens)
    while len(rest) > 1 and (rest[-1] in SALT_WORDS or rest[-1] in IGNORABLE_SALT_WORDS):
        w = rest.pop()
        if w not in IGNORABLE_SALT_WORDS:
            salt.insert(0, w)
    return " ".join(rest), " ".join(salt)


def parse_slash_strengths(text: str, default_unit: Optional[str] = None) -> Optional[List[Optional[dict]]]:
    """'500/125' or '25/12.5 mg' or '500mg/125mg' -> one strength per ingredient (a missing unit takes the unit
    on its right). Returns None if the text is not a plain slash list ('250 mg/5ml' is NOT one: that is per-volume)."""
    t = clean_text(text).strip("() ")
    if not re.fullmatch(r"[\d.]+\s*(?:mg|mcg|iu|g)?(?:\s*/\s*[\d.]+\s*(?:mg|mcg|iu|g)?)+", t):
        return None
    unit, out = default_unit, []
    for p in reversed([p.strip() for p in t.split("/")]):
        pm = re.fullmatch(r"([\d.]+)\s*(mg|mcg|iu|g)?", p)
        unit = pm.group(2) or unit
        mm = STRENGTH_RE.fullmatch(f"{pm.group(1)} {unit}") if unit else None
        out.append(strength_from_match(mm) if mm else None)
    return list(reversed(out))


def make_ingredient(raw_name: str, strength: Optional[dict]) -> Dict[str, Any]:
    n = re.sub(r"[\-_/]", " ", raw_name)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    tokens = n.split()
    moiety, salt = split_salt(tokens)
    valid = (1 <= len(tokens) <= 5 and all(not re.fullmatch(r"[\d.]+", t) for t in tokens)
             and all(len(t) >= 2 or t in ("s", "l", "d", "r", "b", "c", "e", "k") for t in tokens))
    return {"moiety": moiety, "salt": salt, "full_name": " ".join(tokens), "strength": strength, "valid": valid}


def _resolve_parentheses(text: str) -> str:
    """Drop qualifiers '(Diclofenac Diethylamine)', unwrap '(40mg)', use '(A 200mg and B 40mg)' when outer has none."""
    stop = {"tablets", "capsules", "for", "intravenous", "infusion", "origin", "wfi", "per", "and", "the"}
    for _ in range(10):
        m = re.search(r"\(([^()]*)\)", text)
        if not m:
            break
        inner, before, after = m.group(1), text[:m.start()], text[m.end():]
        has_strength = STRENGTH_RE.search(inner) is not None
        words = [w for w in re.findall(r"[a-z][a-z\-]{2,}", STRENGTH_RE.sub(" ", inner)) if w not in stop]
        if has_strength and words:
            if STRENGTH_RE.search(before + " " + after) is None:
                text = inner + " " + after          # 'co-trimoxazole (A 200mg and B 40mg per 5ml) tablets'
            else:
                text = before + " " + after
        elif has_strength:
            text = before + " " + inner + " " + after
        else:
            text = before + " " + after
    return re.sub(r"\s+", " ", text).strip()


def _split_segments(core: str) -> List[str]:
    """Split 'a 5mg and b 10mg' into ingredient segments. Also splits when the list forgot the 'and':
    'Amitriptyline 25mg Chlordiazepoxide 10mg' (a drug name sitting between two strengths starts a new ingredient)."""
    out: List[str] = []
    for seg in [x.strip() for x in re.split(r"\s*(?:\band\b|\+|,|&|;)\s*", core) if x.strip()]:
        ms = list(STRENGTH_RE.finditer(seg))
        cut, last = [], 0
        for a, b in zip(ms, ms[1:]):
            if re.search(r"[a-z]{3,}", seg[a.end():b.start()]):
                cut.append(a.end())
        for c in cut + [len(seg)]:
            part = seg[last:c].strip()
            if part:
                out.append(part)
            last = c
    return out


def analyse_product_text(raw: str) -> Dict[str, Any]:
    """Split a product name into ingredients/strengths/form/release type.
    'complete' is True only if EVERY ingredient has a clean name AND a strength;
    only complete products are used for composition matching."""
    text = apply_spelling(clean_text(raw))
    modifiers = detect_modifiers(text)
    form = detect_form(text)
    forced: Optional[List[dict]] = None

    # "Atenolol + Chlorthalidone Tablet (25/12.5 Mg)" -> strengths given as a slash list
    sm = re.search(r"\(\s*((?:[\d.]+\s*(?:mg|mcg|iu|g)?\s*/\s*)+[\d.]+\s*(?:mg|mcg|iu|g)?)\s*\)", text)
    if sm:
        parts = [p.strip() for p in sm.group(1).split("/")]
        unit = None
        built: List[Optional[dict]] = []
        for p in reversed(parts):               # a missing unit takes the unit to its right
            pm = re.fullmatch(r"([\d.]+)\s*(mg|mcg|iu|g)?", p)
            if not pm:
                built = []
                break
            unit = pm.group(2) or unit
            mm = STRENGTH_RE.fullmatch(f"{pm.group(1)} {unit}") if unit else None
            built.append(strength_from_match(mm) if mm else None)
        forced = list(reversed(built)) if built else None
        text = text[:sm.start()] + " " + text[sm.end():]

    text = _resolve_parentheses(text)
    core = _MOD_ALL.sub(" ", text)
    core = _FORM_WORDS.sub(" ", core)
    core = re.sub(r"\s+", " ", core).strip()

    ingredients: List[Dict[str, Any]] = []
    complete = True
    for seg in _split_segments(core):
        ms = list(STRENGTH_RE.finditer(seg))
        strength = strength_from_match(ms[0]) if ms else None
        name = STRENGTH_RE.sub(" ", seg) if ms else seg          # drop ALL strength/volume numbers from the name
        name = re.sub(r"\b(?:per|w/w|w/v)\b", " ", name)
        ing = make_ingredient(name, strength)
        if not ing["valid"]:
            complete = False
        ingredients.append(ing)

    if forced is not None:                       # slash-list strengths, matched by position
        if len(forced) == len(ingredients) and all(forced):
            for ing, st in zip(ingredients, forced):
                ing["strength"] = st
        else:
            complete = False

    # "A 200mg and B 40mg per 5ml": the trailing 'per' belongs to every ingredient
    if len(ingredients) > 1 and all(i["strength"] for i in ingredients):
        last = ingredients[-1]["strength"]
        if last["per"] and all(i["strength"]["per"] is None and i["strength"]["unit"] in ("mg", "iu", "%")
                               for i in ingredients[:-1]):
            for i in ingredients[:-1]:
                s = dict(i["strength"], per=last["per"], per_unit=last["per_unit"])
                s["text"] += f" per {_num(last['per'])} {last['per_unit']}"
                s["key_text"] = re.sub(r"\s", "", s["text"])
                i["strength"] = s

    if not ingredients or any(i["strength"] is None for i in ingredients) or form == "kit":
        complete = False
    return {"ingredients": ingredients, "complete": complete, "modifiers": modifiers, "form": form, "core": core}


def make_name_key(ingredients: List[dict]) -> Optional[str]:
    """Order-independent key for Level-1 matching: 'caffeine 25mg&paracetamol 500mg'."""
    if not ingredients or any(i["strength"] is None or not i["valid"] for i in ingredients):
        return None
    return "&".join(sorted(f"{i['full_name']} {i['strength']['key_text']}" for i in ingredients))


def composition_key(ingredients: List[dict]) -> str:
    return "&".join(sorted(f"{i['moiety']}|{_strength_key(i['strength'])}" for i in ingredients))


def ingredient_set_key(ingredients: List[dict]) -> str:
    return "&".join(sorted(i["moiety"] for i in ingredients))


# =============================================================================
# 3. READING THE PDF
# =============================================================================
# The list has these columns: Sr. No. | Drug Code | Generic Name | Unit Size | MRP (in Rs.)
# There is NO separate salt/strength/form column: all of that is inside "Generic Name".
START_RE = re.compile(r"^(\d{1,4})\s+(\d{1,5})(?:\s+(.*))?$")
HEADER_RE = re.compile(r"sr\.?\s*no.*drug\s*code.*generic\s*name", re.I)

# words that can appear in a "Unit Size" cell ("10's", "Vial & Wfi", "15 gm tubes", "2 ml Ampoule" ...)
UNIT_WORDS = {"ml", "gm", "gms", "g", "kg", "vial", "vials", "wfi", "amp", "amp.", "ampoule", "ampoules", "in", "a",
              "bottle", "tin", "jar", "box", "sachet", "strips", "strip", "pack", "packs", "mono", "carton",
              "monocarton", "mono-carton", "monopack", "mono-pack", "with", "and", "of", "x", "diluent", "solvent",
              "catridge", "cartridge", "pre-filled", "prefilled", "syringe", "pen", "kit", "combikit", "combipack",
              "pair", "one", "two", "three", "four", "five", "ten", "pcs", "pc", "tube", "tubes", "lami", "lemi",
              "flip", "top", "drops", "drop", "glass", "plastic", "container", "hdpe", "tetra", "screw", "cap",
              "scoop", "&", "mdi", "md", "pouch", "poly", "sealed", "water", "for", "reconstitution", "measuring",
              "applicator", "transfer", "canula", "fluid", "iv", "bag", "amber", "coloured", "powder", "bar", "lami-tube"}
UNIT_START = {"vial", "vials", "pair", "kit", "one", "two", "three", "four", "five", "ten", "pack", "amp",
              "combipack", "combikit", "pouch"}
_QTY = re.compile(r"^\d+(?:\.\d+)?(?:ml|gm|gms|g|kg|'s|s|mdi|md)?$")


def _tok(t: str) -> str:
    return t.lower().strip("().,;:")


def split_price(text: str) -> Tuple[Optional[float], str, Optional[str]]:
    """The MRP is the number at the very end. It can be glued to the unit ("10's5", "Vial0")."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*$", text)
    if not m:
        return None, text, None
    return float(m.group(1)), text[:m.start()].strip(), m.group(1)


def split_unit(rest: str) -> Tuple[str, Optional[str]]:
    """Separate '<name> <unit size>' by walking from the right while words look like unit words."""
    tokens = rest.split()
    i, seen_qty, prev = len(tokens) - 1, False, None
    while i >= 0:
        t = _tok(tokens[i])
        if _QTY.match(t):
            if seen_qty and prev != "x":
                break
            seen_qty = True
        elif t not in UNIT_WORDS:
            break
        prev = t
        i -= 1
    start = i + 1
    while start < len(tokens) and not (_QTY.match(_tok(tokens[start])) or _tok(tokens[start]) in UNIT_START):
        start += 1
    if start >= len(tokens):
        return rest, None
    return " ".join(tokens[:start]).strip(), " ".join(tokens[start:]).strip()


def records_from_lines(lines: List[str], max_gap: int = 20) -> List[dict]:
    """Group text lines into rows. A row starts with '<Sr. No.> <Drug Code>' where Sr. No. counts up 1,2,3...
    (that check stops a wrapped name line from being mistaken for a new row)."""
    records, cur, expected = [], None, 1
    for line in lines:
        line = line.strip()
        if not line or HEADER_RE.search(line) or line.startswith("*"):
            continue                                   # blanks, repeated headers, '*Conditions apply'
        m = START_RE.match(line)
        if m and expected <= int(m.group(1)) <= expected + max_gap:
            if cur:
                records.append(cur)
            sr = int(m.group(1))
            if sr != expected:
                log.warning("Serial number jumped from %d to %d (rows may be missing)", expected - 1, sr)
            expected = sr + 1
            cur = {"sr_no": sr, "drug_code": m.group(2), "parts": [m.group(3)] if m.group(3) else []}
        elif cur is not None:
            cur["parts"].append(line)
    if cur:
        records.append(cur)
    return records


def make_entry(sr_no: int, drug_code: str, name: str, unit: Optional[str], mrp: Optional[float],
               mrp_raw: Optional[str]) -> Dict[str, Any]:
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\b(?!vitamin\b)([A-Za-z]{6,}[nr]) (e)\b", r"\1\2", name)   # word split by the PDF's column wrap
    an = analyse_product_text(name)
    entry = {
        "sr_no": sr_no, "drug_code": str(drug_code), "generic_name": name, "unit_size": unit,
        "mrp": mrp if (mrp is not None and mrp > 0) else None,        # the list prints 0 when no price is set
        "mrp_listed_as_zero": mrp == 0, "mrp_raw": mrp_raw,
        "category": "non_drug" if int(drug_code) >= 5000 else "product",   # 5001+ = devices/consumables
        "dosage_form": an["form"], "modifiers": sorted(an["modifiers"]),
        "composition_complete": an["complete"] and int(drug_code) < 5000,
        "ingredients": an["ingredients"], "name_key": make_name_key(an["ingredients"]),
    }
    return entry


def entry_from_record(rec: dict) -> Dict[str, Any]:
    text = re.sub(r"\s+", " ", " ".join(rec["parts"])).strip()
    mrp, rest, raw = split_price(text)
    name, unit = split_unit(rest)
    e = make_entry(rec["sr_no"], rec["drug_code"], name, unit, mrp, raw)
    if raw is None:
        e["parse_warning"] = "no price found"
    return e


# --- layout-aware reader (the real PDF has no ruling lines, so columns are found by x-position) ---
COL_CODE_MIN, COL_NAME_MIN, COL_UNIT_MIN, COL_MRP_MIN = 120.0, 232.0, 352.0, 480.0   # points, A4 page
ROW_GAP_MIN = 3.0        # a gap this much bigger than the normal line step separates two rows


def _join_lines(words: List[dict]) -> str:
    """Join words top-to-bottom, left-to-right. 'Gastro-' + 'resistant' -> 'Gastro-resistant'."""
    lines: List[List[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    out = ""
    for ln in lines:
        txt = " ".join(w["text"] for w in sorted(ln, key=lambda w: w["x0"]))
        if out and re.search(r"[A-Za-z]-$", out):
            out += txt
        else:
            out += (" " if out else "") + txt
    return re.sub(r"\s+", " ", out).strip()


def _split_rows_on_page(words: List[dict]) -> List[Tuple[dict, List[dict]]]:
    """Return [(anchor, words_of_that_row)] for one page.
    Anchor = a Sr.No. + Drug Code pair on the same line. Cells are vertically centred, so a wrapped name has
    lines both above and below its anchor; the row boundary is the biggest vertical gap between two anchors."""
    anchors = []
    for w in words:
        if w["x1"] < COL_CODE_MIN and w["text"].isdigit():
            mates = [c for c in words if COL_CODE_MIN <= c["x0"] < COL_NAME_MIN and c["text"].isdigit()
                     and abs(c["top"] - w["top"]) <= 2.5]
            if mates:
                anchors.append({"top": w["top"], "sr": int(w["text"]), "code": mates[0]["text"]})
    anchors.sort(key=lambda a: a["top"])
    body = [w for w in words if w["x0"] >= COL_NAME_MIN]
    bounds = [-1e9]
    for a, b in zip(anchors, anchors[1:]):
        tops = sorted({round(w["top"], 1) for w in body if a["top"] - 1 <= w["top"] <= b["top"] + 1}
                      | {round(a["top"], 1), round(b["top"], 1)})
        gaps = [(t2 - t1, (t1 + t2) / 2) for t1, t2 in zip(tops, tops[1:])]
        bounds.append(max(gaps)[1] if gaps else (a["top"] + b["top"]) / 2)
    bounds.append(1e9)
    rows = []
    for k, a in enumerate(anchors):
        rows.append((a, [w for w in body if bounds[k] <= w["top"] < bounds[k + 1]]))
    return rows


def _pdf_layout_entries(pdf_path: str) -> List[Dict[str, Any]]:
    """Read the table using word coordinates: Sr.No | Drug Code | Generic Name | Unit Size | MRP."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber is required:  pip install pdfplumber")
    entries: List[Dict[str, Any]] = []
    expected = 1
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            drop = {round(w["top"]) for w in words if w["text"] in ("Sr.", "Sr") and w["x0"] < COL_CODE_MIN}
            drop |= {round(w["top"]) for w in words if w["text"].startswith("*")}   # '*Conditions apply' footer
            words = [w for w in words if round(w["top"]) not in drop]                # + repeated header row
            for anchor, row in _split_rows_on_page(words):
                if anchor["sr"] != expected:
                    log.warning("Serial number jumped from %d to %d (rows may be missing)", expected - 1, anchor["sr"])
                expected = anchor["sr"] + 1
                name = _join_lines([w for w in row if w["x0"] < COL_UNIT_MIN])
                unit = _join_lines([w for w in row if COL_UNIT_MIN <= w["x0"] < COL_MRP_MIN]) or None
                mrp_txt = _join_lines([w for w in row if w["x0"] >= COL_MRP_MIN])
                ok = re.fullmatch(r"\d+(?:\.\d+)?", mrp_txt or "x") is not None
                entries.append(make_entry(anchor["sr"], anchor["code"], name, unit,
                                          float(mrp_txt) if ok else None, mrp_txt if ok else None))
    return entries


def _pdf_text_lines(pdf_path: str) -> List[str]:
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber is required:  pip install pdfplumber")
    lines: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            lines.extend((page.extract_text() or "").splitlines())
    return lines


def _ocr_lines(pdf_path: str) -> List[str]:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError("This PDF has no text layer (scanned). For OCR run: pip install pytesseract pdf2image "
                           "and install the Tesseract + Poppler programs.")
    lines: List[str] = []
    for img in convert_from_path(pdf_path, dpi=300):
        lines.extend(pytesseract.image_to_string(img, config="--psm 6").splitlines())
    return lines


def load_janaushadhi_database(path: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse the Jan Aushadhi PDF (or a .txt of its extracted text) -> (entries, method used)."""
    if path.lower().endswith(".txt"):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        return [entry_from_record(r) for r in records_from_lines(lines)], "text-file"

    text_lines = _pdf_text_lines(path)
    if sum(len(l) for l in text_lines) < 500:
        log.warning("No text layer found - trying OCR (results need checking: run --inspect)")
        return [entry_from_record(r) for r in records_from_lines(_ocr_lines(path))], "ocr"
    entries = _pdf_layout_entries(path)
    if len(entries) < 50:   # layout of a different PDF: fall back to plain text lines
        log.warning("Column layout not recognised - falling back to plain text lines (less reliable)")
        return [entry_from_record(r) for r in records_from_lines(text_lines)], "pdf-text-lines"
    return entries, "pdf-layout"


# =============================================================================
# 4. THE MATCHER
# =============================================================================
class MedicineMatcher:
    """
    matcher = MedicineMatcher("Product List-1.pdf")
    result  = matcher.find_match({"medicine_name": "Dolo 650", "strength": "650 mg",
                                  "dosage_form": "tablet", "salts": ["Paracetamol"]})
    full    = matcher.process_prescription([...])

    composition_lookup (optional): a function  name -> list of salts (e.g. ["Paracetamol"]) or None.
    It is only called when the prescription gives no salts AND the name is not a known ingredient.
    """

    def __init__(self, pdf_path: str, cache_path: Optional[str] = None, rebuild_cache: bool = False,
                 composition_lookup: Optional[Callable[[str], Optional[List[str]]]] = None, debug: bool = False):
        if debug:
            logging.basicConfig(level=logging.INFO, format="%(message)s")
            log.setLevel(logging.INFO)
        self.pdf_path = pdf_path
        self.cache_path = cache_path or os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                                                      "janaushadhi_cache.json")
        self.composition_lookup = composition_lookup
        self.entries: List[Dict[str, Any]] = []
        self.parse_method = "cache"
        self._load(rebuild_cache)
        self._build_indexes()

    # ---- loading / caching -------------------------------------------------
    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _load(self, rebuild: bool) -> None:
        pdf_exists = os.path.exists(self.pdf_path)
        digest = self._sha256(self.pdf_path) if pdf_exists else None
        if not rebuild and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
                fresh = cache.get("parser_version") == PARSER_VERSION and (digest is None or cache.get("pdf_sha256") == digest)
                if fresh:
                    self.entries = cache["entries"]
                    log.info("Loaded %d products from cache %s", len(self.entries), self.cache_path)
                    return
                log.info("Cache is out of date (PDF or parser changed) - rebuilding")
            except (OSError, ValueError, KeyError):
                log.info("Cache unreadable - rebuilding")
        if not pdf_exists:
            raise FileNotFoundError(f"Jan Aushadhi PDF not found: {self.pdf_path}")
        self.entries, self.parse_method = load_janaushadhi_database(self.pdf_path)
        if not self.entries:
            raise RuntimeError("No products could be read from the PDF. Run: python matcher.py <pdf> --inspect")
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({"parser_version": PARSER_VERSION, "pdf_sha256": digest, "pdf_name": os.path.basename(self.pdf_path),
                       "method": self.parse_method, "entries": self.entries}, f, ensure_ascii=False)
        log.info("Parsed %d products (%s) and saved cache %s", len(self.entries), self.parse_method, self.cache_path)

    def _build_indexes(self) -> None:
        self.name_index: Dict[str, List[int]] = {}
        self.composition_strength_index: Dict[str, List[int]] = {}
        self.ingredient_index: Dict[str, List[int]] = {}
        self.vocab: set = set()
        for idx, e in enumerate(self.entries):
            if e["category"] == "non_drug":
                continue
            for ing in e["ingredients"]:
                if ing["valid"]:
                    self.vocab.add(ing["moiety"])
            if not e["composition_complete"]:
                if e["name_key"]:
                    self.name_index.setdefault(e["name_key"], []).append(idx)
                continue
            if e["name_key"]:
                self.name_index.setdefault(e["name_key"], []).append(idx)
            self.composition_strength_index.setdefault(composition_key(e["ingredients"]), []).append(idx)
            self.ingredient_index.setdefault(ingredient_set_key(e["ingredients"]), []).append(idx)

    # ---- public API -----------------------------------------------------------
    def process_prescription(self, medicines: List[dict]) -> Dict[str, Any]:
        results = [self.find_match(m) for m in medicines]
        matched = [r for r in results if r["matched"]]
        return {
            "results": results,
            "summary": {"total": len(results), "matched": len(matched),
                        "matched_needing_verification": sum(1 for r in matched if r["manual_verification_required"]),
                        "not_matched": len(results) - len(matched)},
            "advice": DISCLAIMER,
        }

    def find_match(self, medicine: dict) -> Dict[str, Any]:
        rx = self._prepare_rx(medicine)
        label = rx["name"] or ", ".join(i["full_name"] for i in rx["ingredients"]) or "(unnamed)"
        log.info("\nSearching: %r", label)
        if not rx["name"] and not rx["ingredients"]:
            return self._no_match(rx, "invalid_input", "Neither a medicine name nor salts were supplied.")

        # no usable composition (e.g. a brand name with no salts)
        if not rx["ingredients"] or (rx["source"] == "medicine_name" and not self._all_known(rx["ingredients"])
                                     and not self._fuzzy_fix(rx["ingredients"])[1]):
            log.info("Composition: NOT AVAILABLE -> composition_required")
            return self._no_match(rx, "composition_required",
                                  "The composition (salts) of this medicine is not known. Provide the 'salts' field "
                                  "(or plug in a trusted composition lookup); the matcher will not guess it.")

        if any(i["strength"] is None for i in rx["ingredients"]):
            log.info("Strength: MISSING -> strength_missing")
            return self._no_match(rx, "strength_missing",
                                  "Strength is missing or unreadable (strength_missing), so no product can be matched "
                                  "safely. The Jan Aushadhi strengths available for these ingredients are listed.")

        # ---- Level 1: exact name ------------------------------------------------
        key = rx["name_key"]
        if key:
            hits = self.name_index.get(key, [])
            log.info("Exact name match (%s): %s", key, "FOUND %d" % len(hits) if hits else "NOT FOUND")
            res = self._pick(rx, hits, BASE_EXACT_NAME, "exact_name")
            if res:
                return res
        else:
            log.info("Exact name match: skipped (name does not look like ingredient + strength)")

        # ---- Level 2: composition -----------------------------------------------
        log.info("Composition: %s", " + ".join(i["strength"]["text"] + " " + i["full_name"] for i in rx["ingredients"]))
        res = self._composition_search(rx, BASE_COMPOSITION, "composition_match")
        if res:
            return res

        # ---- Level 3: controlled fuzzy (typos in ingredient names only) -------------
        fixed, changed = self._fuzzy_fix(rx["ingredients"])
        if changed:
            rx2 = dict(rx, ingredients=fixed, fuzzy_corrections=changed)
            log.info("Fuzzy: %s", ", ".join(f"{a} -> {b}" for a, b in changed))
            res = self._composition_search(rx2, min(FUZZY_MAX, BASE_COMPOSITION) - FUZZY_PENALTY * len(changed),
                                           "fuzzy_composition_match")
            if res:
                return res

        unknown = [i["full_name"] for i in rx["ingredients"] if i["moiety"] not in self.vocab]
        if unknown:
            return self._no_match(rx, "no_match", "Active ingredient(s) not found in the Jan Aushadhi list: "
                                  + ", ".join(unknown) + ".")
        return self._no_match(rx, "no_match", "Jan Aushadhi has no product with exactly these ingredients, strengths and "
                              "dosage form. A near miss is NOT offered as a match.")

    # ---- preparing the prescription side -----------------------------------------
    def _prepare_rx(self, med: dict) -> Dict[str, Any]:
        name = str(med.get("medicine_name") or "").strip()
        strength_txt = clean_text(med.get("strength"))
        form_txt = str(med.get("dosage_form") or "")
        salts_in = med.get("salts") or med.get("composition") or []
        if isinstance(salts_in, str):
            salts_in = [salts_in]
        warnings: List[str] = []
        source, ings = None, []

        salt_texts: List[str] = []
        for s in salts_in:
            salt_texts.append(f"{s.get('name', '')} {s.get('strength', '')}" if isinstance(s, dict) else str(s))
            ings.extend(analyse_product_text(salt_texts[-1])["ingredients"])
        if ings:
            source = "salts"

        name_an = analyse_product_text(re.sub(r"\s+\d+(?:\.\d+)?\s*$", "", name)) if name else None
        if not ings and name_an and all(i["valid"] for i in name_an["ingredients"]):
            ings, source = name_an["ingredients"], "medicine_name"
        if not ings and name and self.composition_lookup:
            looked = self.composition_lookup(name)
            if looked:
                for s in looked:
                    ings.extend(analyse_product_text(str(s))["ingredients"])
                source = "lookup"
                warnings.append("Composition came from an external lookup, not the prescription - verify it.")
        ings = [i for i in ings if i["valid"]]

        # strengths from the 'strength' field, matched to ingredients by position
        missing = [i for i in ings if i["strength"] is None]
        slash = parse_slash_strengths(strength_txt)
        force_manual = False
        if slash and not all(slash) and len(slash) == len(missing) >= 2:
            guessed = parse_slash_strengths(strength_txt, "mg")      # bare '500/125': no unit written anywhere
            if guessed and all(guessed):
                slash, force_manual = guessed, True
                warnings.append(f"Strength '{strength_txt}' has no unit; mg was ASSUMED. Verify it.")
        if slash and len(slash) == len(missing) >= 2 and all(slash):
            field_strengths = slash                       # '500/125' for two salts
        else:
            field_strengths = [strength_from_match(m) for m in STRENGTH_RE.finditer(strength_txt)]
        if field_strengths and missing:
            if len(field_strengths) == len(missing):
                for i, s in zip(missing, field_strengths):
                    i["strength"] = s
                if len(missing) > 1:
                    warnings.append("Strengths were assigned to salts in the order given.")
            elif len(ings) == 1:
                ings[0]["strength"] = field_strengths[0]
                warnings.append("Several strengths were given for one ingredient; the first was used.")
            else:
                warnings.append("Could not tell which strength belongs to which salt.")
        elif strength_txt and not field_strengths:
            warnings.append(f"Strength '{strength_txt}' has no recognisable unit (mg, mcg, g, IU, %).")

        form = detect_form(form_txt) or detect_form(name)
        mods = detect_modifiers(form_txt) | detect_modifiers(name) | detect_modifiers(" ".join(salt_texts))
        name_key = None
        if name_an and name_an["ingredients"]:
            nm = [dict(i) for i in name_an["ingredients"]]
            if len(nm) == 1 and nm[0]["strength"] is None and ings and ings[0]["strength"]:
                nm[0]["strength"] = ings[0]["strength"]
            name_key = make_name_key(nm)
        return {"name": name, "ingredients": ings, "source": source, "form": form, "modifiers": mods,
                "name_key": name_key, "warnings": warnings, "fuzzy_corrections": [], "force_manual": force_manual}

    # ---- fuzzy typo correction ------------------------------------------------------
    def _all_known(self, ings: List[dict]) -> bool:
        return all(i["moiety"] in self.vocab for i in ings)

    def _fuzzy_fix(self, ings: List[dict]) -> Tuple[List[dict], List[Tuple[str, str]]]:
        """Fix a misspelt ingredient only if EXACTLY ONE listed ingredient is within 1 edit (2 for long
        names). Edit distance (not similarity %) is used because e.g. 'prednisone' vs 'prednisolone'
        look 90% similar but are different drugs."""
        if Levenshtein is None:
            return ings, []
        out, changed = [], []
        for i in ings:
            m = i["moiety"]
            if m in self.vocab or len(m) < 6:
                out.append(i)
                continue
            limit = 1 if len(m) < 12 else 2
            near = [v for v in self.vocab if v[0] == m[0] and abs(len(v) - len(m)) <= limit
                    and Levenshtein.distance(m, v) <= limit]
            if len(near) == 1:
                fixed = dict(i, moiety=near[0], full_name=(near[0] + " " + i["salt"]).strip())
                out.append(fixed)
                changed.append((m, near[0]))
            else:
                out.append(i)
        return out, changed

    # ---- candidate search & scoring ---------------------------------------------------
    def _composition_search(self, rx: dict, base: float, match_type: str) -> Optional[Dict[str, Any]]:
        hits = self.composition_strength_index.get(composition_key(rx["ingredients"]), [])
        log.info("Searching composition+strength index... %d candidate(s)", len(hits))
        res = self._pick(rx, hits, base, match_type)
        if res:
            return res
        hits = self.ingredient_index.get(ingredient_set_key(rx["ingredients"]), [])
        log.info("Searching ingredient index... %d candidate(s)", len(hits))
        return self._pick(rx, hits, base, match_type)

    def _evaluate(self, rx: dict, e: dict, base: float) -> Optional[Dict[str, Any]]:
        """Compare one Jan Aushadhi product with the prescription. None = rejected."""
        if not e["composition_complete"]:
            return None
        ja = sorted(e["ingredients"], key=lambda i: (i["moiety"], _strength_key(i["strength"])))
        rxi = sorted(rx["ingredients"], key=lambda i: (i["moiety"], _strength_key(i["strength"])))
        if [i["moiety"] for i in ja] != [i["moiety"] for i in rxi]:
            return None
        score, notes, hard = base, [], False
        details = {"ingredient_match": True, "strength_match": True}
        for r, j in zip(rxi, ja):
            if r["salt"] and j["salt"] and r["salt"] != j["salt"]:
                log.info("  Candidate %s: salt differs (%s vs %s) -> reject", e["generic_name"], r["salt"], j["salt"])
                return None
            if r["salt"] and not j["salt"]:
                score -= DED_SALT_JA_MISSING; hard = True
                notes.append(f"Prescription names the salt '{r['salt']}' for {r['moiety']} but the list does not state one.")
            elif j["salt"] and not r["salt"]:
                score -= DED_SALT_RX_MISSING
                notes.append(f"Salt of {r['moiety']} not given in the prescription (list product: {j['salt']}).")
            cmp = compare_strength(r["strength"], j["strength"])
            if cmp in ("different", "missing"):
                log.info("  Candidate %s: strength differs -> reject", e["generic_name"])
                return None
            if cmp == "per_unverified":
                score -= DED_PER_UNVERIFIED; hard = True; details["strength_match"] = "per_volume_unverified"
                notes.append(f"{r['moiety']}: amount matches but concentration/volume basis could not be verified "
                             f"(list: {j['strength']['text']}).")
        # dosage form
        if not rx["form"]:
            score -= DED_FORM_MISSING; details["dosage_form_match"] = "not_verified"
            notes.append("Dosage form was not given in the prescription, so it could not be verified.")
        elif not e["dosage_form"]:
            score -= DED_FORM_MISSING; details["dosage_form_match"] = "not_verified"
            notes.append("The list does not state a dosage form for this product.")
        else:
            fc = form_compat(rx["form"], e["dosage_form"])
            if fc == "different":
                log.info("  Candidate %s: dosage form %s != %s -> reject", e["generic_name"], rx["form"], e["dosage_form"])
                return None
            details["dosage_form_match"] = "yes" if fc == "same" else "partial"
            if fc == "partial":
                score -= DED_FORM_PARTIAL; hard = True
                notes.append(f"Dosage form is similar but not identical (prescribed {rx['form']}, list {e['dosage_form']}).")
        # release type (SR / enteric coated / dispersible ...)
        jm, rm = set(e["modifiers"]), set(rx["modifiers"])
        if rm == jm:
            details["release_type_match"] = "yes"
        elif rm and not jm:
            log.info("  Candidate %s: prescription needs %s release, list product has none -> reject", e["generic_name"], rm)
            return None
        elif not rm:
            extra = jm - rm
            score -= max(DED_RELEASE.get(m, 0.10) for m in extra)
            hard = hard or any(m != "delayed" for m in extra)
            details["release_type_match"] = "not_verified"
            notes.append("List product is " + ", ".join(sorted(extra)) + " release; the prescription does not say.")
        else:
            return None
        score = max(0.0, min(score, FUZZY_MAX if rx["fuzzy_corrections"] else 0.99))
        return {"entry": e, "confidence": round(score, 2), "notes": notes, "hard_flag": hard, "details": details}

    def _pick(self, rx: dict, idxs: List[int], base: float, match_type: str) -> Optional[Dict[str, Any]]:
        evals = []
        for idx in dict.fromkeys(idxs):
            ev = self._evaluate(rx, self.entries[idx], base)
            if ev:
                log.info("  Candidate %s: ingredient YES, strength YES, form %s, confidence %.2f",
                         ev["entry"]["generic_name"], ev["details"].get("dosage_form_match"), ev["confidence"])
                evals.append(ev)
        if not evals:
            return None
        evals.sort(key=lambda v: (-v["confidence"], v["entry"]["sr_no"]))
        best = evals[0]
        if best["confidence"] < MIN_CONFIDENCE:
            log.info("  Best confidence %.2f is below %.2f -> rejected", best["confidence"], MIN_CONFIDENCE)
            return None
        ties = [v for v in evals[1:] if abs(v["confidence"] - best["confidence"]) < 0.005]
        return self._success(rx, best, ties, match_type)

    # ---- building the output --------------------------------------------------------------
    @staticmethod
    def _product_ref(e: dict) -> Dict[str, Any]:
        return {"janaushadhi_product": e["generic_name"], "drug_code": e["drug_code"],
                "mrp": e["mrp"], "pack_size": e["unit_size"]}

    def _success(self, rx: dict, best: dict, ties: List[dict], match_type: str) -> Dict[str, Any]:
        e, conf = best["entry"], best["confidence"]
        warnings = list(rx["warnings"]) + best["notes"]
        manual = (conf < MANUAL_BELOW or best["hard_flag"] or bool(rx["fuzzy_corrections"])
                  or rx.get("force_manual", False))
        if rx["fuzzy_corrections"]:
            warnings.append("Ingredient spelling was corrected: "
                            + ", ".join(f"'{a}' -> '{b}'" for a, b in rx["fuzzy_corrections"]) + ". Confirm it.")
        if rx["source"] == "medicine_name" and match_type != "exact_name":
            warnings.append("Composition was read from the medicine name itself (no salts supplied).")
            manual = True
        if e["mrp"] is None:
            warnings.append("The Jan Aushadhi list shows no price (0) for this product.")
        alternatives = [self._product_ref(v["entry"]) for v in ties]
        if alternatives:
            warnings.append("More than one list entry fits equally well (different pack sizes/codes); see 'alternatives'.")
        composition = [f"{i['full_name'].title()} {i['strength']['text']}" for i in e["ingredients"]]
        rx_label = rx["name"] or " + ".join(i["full_name"] for i in rx["ingredients"])
        if match_type == "exact_name":
            why = "Prescribed name, strength and dosage form match a Jan Aushadhi product name."
        elif match_type == "composition_match":
            why = ("Exact medicine name not found. All active ingredients, their strengths and the dosage form "
                   "match a Jan Aushadhi product.")
        else:
            why = ("Medicine name not found exactly. After correcting a probable spelling error, all active ingredients "
                   "and strengths match a Jan Aushadhi product.")
        if best["notes"]:
            why += " Caveats: " + " ".join(best["notes"])
        log.info("Final confidence: %.2f  manual verification: %s", conf, manual)
        return {
            "matched": True, "status": "potential_match" if manual else "matched",
            "match_type": match_type, "confidence": conf,
            "prescribed_medicine": rx_label,
            "generic_name": e["generic_name"],
            "composition": composition,
            "strength": " + ".join(i["strength"]["text"] for i in e["ingredients"]),
            "dosage_form": pretty_form(e["dosage_form"]),
            "release_type": e["modifiers"] or None,
            "janaushadhi_product": e["generic_name"], "drug_code": e["drug_code"],
            "mrp": e["mrp"], "pack_size": e["unit_size"],
            "match_details": best["details"], "alternatives": alternatives, "warnings": warnings,
            "reason": why, "manual_verification_required": manual, "advice": DISCLAIMER,
        }

    def _no_match(self, rx: dict, status: str, reason: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "matched": False, "status": status, "match_type": None, "confidence": 0,
            "prescribed_medicine": rx["name"] or None, "reason": reason, "manual_verification_required": True,
            "warnings": list(rx["warnings"]),
        }
        if rx["ingredients"] and status in ("strength_missing", "no_match") and self._all_known(rx["ingredients"]):
            idxs = self.ingredient_index.get(ingredient_set_key(rx["ingredients"]), [])
            if idxs:
                out["same_ingredients_available_in_list"] = [dict(self._product_ref(self.entries[i]),
                                                                  dosage_form=pretty_form(self.entries[i]["dosage_form"]))
                                                             for i in idxs[:8]]
            else:
                out["same_ingredients_available_in_list"] = []
        log.info("Result: %s", status)
        return out


# =============================================================================
# 5. COMMAND LINE HELPERS
# =============================================================================
def _inspect(pdf_path: str) -> None:
    """Show how the PDF was read - run this first on a new PDF."""
    entries, method = load_janaushadhi_database(pdf_path)
    print(f"Method: {method}   products read: {len(entries)}")
    if not entries:
        return
    print(f"Serial numbers: {entries[0]['sr_no']} .. {entries[-1]['sr_no']}")
    print(f"No price found: {sum(1 for e in entries if e['mrp_raw'] is None)}   "
          f"price listed as 0: {sum(1 for e in entries if e['mrp_listed_as_zero'])}")
    print(f"Composition fully parsed: {sum(1 for e in entries if e['composition_complete'])} "
          f"(others can only be matched by name)")
    print(f"No unit size found: {sum(1 for e in entries if not e['unit_size'])}")
    print("\nFirst 8 rows as parsed:")
    for e in entries[:8]:
        print(f"  #{e['sr_no']} code {e['drug_code']} | {e['generic_name']} | unit={e['unit_size']} | mrp={e['mrp']} "
              f"| form={e['dosage_form']} | mods={e['modifiers']}")


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Jan Aushadhi matcher")
    ap.add_argument("pdf")
    ap.add_argument("--inspect", action="store_true", help="show how the PDF is parsed")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the JSON cache")
    ap.add_argument("--name", default="")
    ap.add_argument("--strength", default="")
    ap.add_argument("--form", default="")
    ap.add_argument("--salts", nargs="*", default=[])
    a = ap.parse_args(argv)
    if a.inspect:
        _inspect(a.pdf)
        return
    m = MedicineMatcher(a.pdf, rebuild_cache=a.rebuild, debug=True)
    if a.name or a.salts:
        print(json.dumps(m.find_match({"medicine_name": a.name, "strength": a.strength, "dosage_form": a.form,
                                       "salts": a.salts}), indent=2, ensure_ascii=False))
    elif a.rebuild:
        print(f"Cache rebuilt: {len(m.entries)} products")


if __name__ == "__main__":
    main()
