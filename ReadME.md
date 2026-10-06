# 💊 DawaRx — AI Prescription Decipherer & Generic Finder

> Upload a photo of your handwritten Indian doctor's prescription. DawaRx uses Google Gemini Vision to extract medicines and finds cheaper **Jan Aushadhi (PMBJP) generic alternatives** with price savings — all in seconds.

---

## Architecture

```
[Next.js UI] ──multipart image──▶ [FastAPI /api/analyze]
                                         │
                     1. Pillow: resize ≤1600px, fix EXIF, convert JPEG
                     2. Gemini Vision → structured JSON (medicines[])
                     3. Per medicine:
                          a. Normalize name (strip form/dosage tokens)
                          b. Fuzzy match → Brand DB (RapidFuzz WRatio)
                             → composition / strength / price
                          c. Fuzzy match composition → Jan Aushadhi list
                             → generic name / MRP
                          d. Compute savings
                     4. Return combined JSON with confidence scores
```

## Folder Structure

```
backend/
  app/
    main.py          # FastAPI app, CORS, startup data loading
    config.py        # Pydantic-settings env config
    schemas.py       # All Pydantic v2 models
    routes/
      analyze.py     # POST /api/analyze  GET /api/search  GET /api/health
    services/
      vision.py      # Gemini Vision + mock fallback
      preprocess.py  # Pillow image cleanup
      normalizer.py  # Name/dosage normalization
      matcher.py     # RapidFuzz brand→generic pipeline
      pricing.py     # Savings calculation
    data/
      A_Z_medicines_dataset_of_India.csv     # ~191k brands
      sarkariyojana.com-jan-aushadhi-*.csv   # ~1.6k Jan Aushadhi generics
  tests/
    test_normalizer.py
    test_matcher.py

frontend/
  app/
    page.tsx         # Single-page app
    layout.tsx       # Root layout + SEO metadata
    globals.css      # Full design system
  components/
    UploadCard.tsx
    MedicineCard.tsx
    SavingsBanner.tsx
    ConfidenceBadge.tsx
  lib/api.ts         # Typed fetch client
  types/index.ts     # Shared TypeScript types
```

## Quick Start

### Prerequisites
- Python 3.11+ and `pip`
- Node.js 18+ and `npm`
- A [Google Gemini API key](https://aistudio.google.com/app/apikey) (optional — mock fallback works without it)

### 1. Backend

```bash
cd backend
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=your_key_here

pip install -r requirements.txt
uvicorn app.main:app --reload
# → http://localhost:8000
```

### 2. Frontend

```bash
cd frontend
# .env.local is already set to http://localhost:8000
npm install
npm run dev
# → http://localhost:3000
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | `""` | Google Gemini API key. If empty, mock response is used. |
| `GEMINI_MODEL` | `gemini-1.5-flash-latest` | Model to use for vision extraction. |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend URL for the frontend. |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check + dataset sizes |
| `POST` | `/api/analyze` | Analyze prescription image (multipart `file`) |
| `GET` | `/api/search?q=` | Fuzzy search both databases for manual correction |

## Running Tests

```bash
cd backend
python -m pytest tests/ -v
# 25 passed
```

## Data Sources

| Dataset | Records | Source |
|---|---|---|
| A-Z Medicines India | ~191,637 | Kaggle — Indian medicines with brand name, composition, price |
| Jan Aushadhi (PMBJP) | ~1,629 | sarkariyojana.com / janaushadhi.gov.in product list |

## Known Limitations & Future Improvements

- **Gemini OCR accuracy** depends heavily on image quality. Very faint or smudged handwriting may produce low-confidence results.
- **Brand prices** in the dataset may be outdated; savings figures are indicative only.
- **No user session / history** — all processing is stateless and images are never stored.
- **Strength matching** is best-effort: if the Jan Aushadhi list has no entry for the exact strength, the closest compositional match is shown with a warning.
- **Future**: Add PDF prescription support, drug interaction warnings, and nearby Jan Aushadhi store locator.

## Disclaimer

> AI-generated interpretation. **Always confirm with your doctor or pharmacist** before purchasing or substituting any medicine.
