"""Unit tests for app.services.normalizer."""
import pytest
from app.services.normalizer import normalize_name, normalize_composition, extract_strength


class TestNormalizeName:
    def test_strips_form_token(self):
        result = normalize_name("Tab Dolo 650")
        assert "dolo" in result
        assert "tab" not in result
        assert "650" not in result

    def test_strips_dosage(self):
        assert normalize_name("Dolo 650mg") == "dolo"

    def test_strips_form_and_dosage(self):
        assert normalize_name("Tab. Dolo 650mg") == "dolo"

    def test_augmentin_duo(self):
        result = normalize_name("Augmentin 625 Duo Tablet")
        assert "augmentin" in result
        assert "duo" in result

    def test_pan_40_hyphen_preserved(self):
        result = normalize_name("Pan-40 Cap")
        assert "pan-40" in result

    def test_lowercase(self):
        assert normalize_name("CROCIN") == "crocin"

    def test_syrup_stripped(self):
        result = normalize_name("Syrup Benadryl 100ml")
        assert "benadryl" in result
        assert "syrup" not in result

    def test_injection_stripped(self):
        result = normalize_name("Inj Tramadol 50mg")
        assert "tramadol" in result

    def test_empty_string(self):
        assert normalize_name("") == ""


class TestNormalizeComposition:
    def test_strips_bracketed_dosage(self):
        result = normalize_composition("Amoxycillin (500mg) + Clavulanic Acid (125mg)")
        assert "amoxycillin" in result
        assert "clavulanic acid" in result
        assert "500" not in result

    def test_lowercase(self):
        assert normalize_composition("Paracetamol") == "paracetamol"

    def test_two_spaces_composition(self):
        result = normalize_composition("Amoxycillin  (500mg) ")
        assert result.strip() == "amoxycillin"


class TestExtractStrength:
    def test_mg(self):
        assert extract_strength("Dolo 650mg") == "650mg"

    def test_mcg(self):
        assert extract_strength("Thyroxine 50mcg tablet") == "50mcg"

    def test_none_when_missing(self):
        assert extract_strength("Dolo") is None

    def test_ml(self):
        assert extract_strength("Lactulose 200ml") == "200ml"
