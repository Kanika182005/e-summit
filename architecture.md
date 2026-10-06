# PROJECT: AI Prescription Decipherer & Generic Finder (12-hour hackathon MVP)

## ROLE
You are a senior full-stack engineer building a hackathon MVP. Prioritize a working end-to-end demo over perfection. Build incrementally, run and test each phase before moving on, and keep the code simple and readable.

## GOAL
User uploads a photo of a handwritten Indian doctor's prescription. The system:
1. Uses Gemini Vision to extract medicines, dosage, frequency, duration into structured JSON.
2. Maps each branded medicine to its salt/composition, then to a cheaper Jan Aushadhi (PMBJP) generic alternative using fuzzy matching.
3. Shows the user a side-by-side comparison with price savings.

## TECH STACK (fixed, do not substitute)
- Backend: Python 3.11+, FastAPI, Uvicorn, Pydantic v2, Pandas, NumPy, RapidFuzz
- OCR/Vision: Google Gemini via the `google-genai` SDK (model name from env var `GEMINI_MODEL`, default a fast vision-capable Flash model). Use structured JSON output (response_mime_type="application/json" with a response schema).
- Frontend: Next.js (App Router) + React + TypeScript + Tailwind CSS
- Config: `.env` files (GEMINI_API_KEY, GEMINI_MODEL, NEXT_PUBLIC_API_URL). Never hardcode keys.
- No database. Load datasets into memory (Pandas) at server startup.

## ARCHITECTURE

```
[Next.js UI] --multipart image--> [FastAPI /api/analyze]
                                      |
                  1. Image preprocess (Pillow: resize max 1600px, fix EXIF rotation, convert JPEG)
                  2. Gemini Vision -> structured JSON (medicines[])
                  3. Matching pipeline per medicine:
                       a. Normalize name (lowercase, strip "tab/cap/syp/inj", dosage, punctuation)
                       b. Fuzzy match against Brand DB (RapidFuzz WRatio / token_set_ratio)
                          -> get composition/salt + strength
                       c. Fuzzy match composition against Jan Aushadhi list
                          -> get generic name, strength, pack size, MRP
                       d. Compute savings vs brand price
                  4. Return combined JSON with confidence scores
```

### Folder structure
```
/backend
  app/
    main.py              # FastAPI app, CORS, startup data loading
    config.py            # env settings
    schemas.py           # Pydantic models
    routes/analyze.py    # POST /api/analyze, GET /api/health, GET /api/search?q=
    services/
      vision.py          # Gemini call + prompt + schema + retry
      preprocess.py      # image cleanup
      normalizer.py      # name/dosage normalization helpers
      matcher.py         # brand->composition->generic matching (RapidFuzz)
      pricing.py         # savings calculation
    data/
      brands.csv         # brand_name, composition, strength, manufacturer, price
      jan_aushadhi.csv   # product_code, generic_name, strength, unit_size, mrp
  tests/
    test_normalizer.py
    test_matcher.py
  requirements.txt
  .env.example
/frontend
  app/page.tsx           # upload + results page
  components/            # UploadCard, MedicineCard, SavingsBanner, ConfidenceBadge, EditableField
  lib/api.ts             # typed API client
  types/index.ts
```

## DATA
- Jan Aushadhi: the official PMBJP product list (janaushadhi.gov.in, "Product List"). Download/convert to `jan_aushadhi.csv`. If it cannot be fetched, generate a clearly-labelled seed CSV of ~60 common medicines (Paracetamol, Amoxicillin, Metformin, Atorvastatin, Pantoprazole, Cetirizine, Azithromycin, Amlodipine, Telmisartan, Omeprazole, etc.) so the demo works, and note it in the README.
- Brand DB: use a public Indian medicines dataset (e.g. a Kaggle "Indian medicine" CSV with brand name + composition). If unavailable, create a seed `brands.csv` of ~150 popular brands (Dolo 650, Crocin, Augmentin, Pan 40, Glycomet, Calpol, Azithral, Allegra, Montair, Ecosprin, etc.) with composition and approximate MRP.
- Clean with Pandas at startup: lowercase, strip, drop duplicates, pre-build normalized lookup columns.

## GEMINI VISION REQUIREMENTS
Prompt Gemini as an expert at reading messy handwritten Indian prescriptions. Output strictly this schema:
```json
{
  "patient_name": "string|null",
  "doctor_name": "string|null",
  "date": "string|null",
  "medicines": [
    {
      "raw_text": "text as written",
      "name": "best-guess medicine name",
      "form": "tablet|capsule|syrup|injection|ointment|drops|other|null",
      "strength": "e.g. 500 mg or null",
      "dosage": "e.g. 1 tablet",
      "frequency": "e.g. 1-0-1 or BD",
      "frequency_plain": "plain English, e.g. Twice a day",
      "timing": "before food|after food|bedtime|null",
      "duration": "e.g. 5 days or null",
      "confidence": 0.0
    }
  ],
  "overall_legibility": "high|medium|low",
  "notes": "string"
}
```
Rules for the prompt: interpret Indian abbreviations (OD, BD, TDS, QID, HS, SOS, AC, PC, Tab, Cap, Syp, Inj, 1-0-1 notation); never invent medicines; set low confidence and keep raw_text when unsure; return null for unreadable fields. Add retry (max 2) with JSON validation via Pydantic and graceful error messages.

## MATCHING REQUIREMENTS
- Use `rapidfuzz.process.extractOne` / `extract` with `fuzz.WRatio` and `token_set_ratio`, scorer cutoff configurable (default 80 for brand match, 75 for generic match).
- Return top 3 candidates per stage with scores, mark the best one, and flag `needs_review=true` if score < threshold or Gemini confidence < 0.6.
- If the prescription already contains a generic/salt name, skip the brand stage and match directly to Jan Aushadhi.
- Match strength too: prefer Jan Aushadhi entries whose strength equals the brand's strength; otherwise show closest and warn.
- Savings = brand_price - generic_price, percent saved; handle missing prices gracefully.

## API CONTRACT
- `POST /api/analyze` (multipart `file`) → 
```json
{
  "prescription": { "patient_name": "...", "doctor_name": "...", "date": "...", "overall_legibility": "..." },
  "medicines": [
    {
      "extracted": { ...gemini fields },
      "brand_match": { "name": "...", "composition": "...", "price": 0, "score": 0, "alternatives": [] },
      "generic_match": { "name": "...", "strength": "...", "mrp": 0, "score": 0, "alternatives": [] },
      "savings": { "amount": 0, "percent": 0 },
      "needs_review": false
    }
  ],
  "total_savings": { "amount": 0, "percent": 0 },
  "disclaimer": "..."
}
```
- `GET /api/search?q=` for manual fuzzy search so users can correct a wrong match.
- `GET /api/health`.
- Enable CORS for localhost:3000. Validate file type (jpg/png/webp) and size (max 8 MB).

## FRONTEND REQUIREMENTS
- Single-page, mobile-first, clean medical-style UI (Tailwind).
- Drag-and-drop / camera upload with image preview; loading state with progress text ("Reading handwriting…", "Finding generics…").
- Results: a card per medicine showing extracted name, dosage, frequency (plain English), duration, confidence badge (green/amber/red), the matched brand → Jan Aushadhi generic, prices, and savings %.
- Fields are editable inline; low-confidence cards are highlighted with a "Please verify" tag and a "Search manually" box that calls `/api/search`.
- Top banner with total estimated savings.
- "Download JSON" and "Copy summary" buttons.
- Always show the disclaimer prominently.
- Handle errors (bad image, API failure, no medicines found) with friendly messages.

## SAFETY & QUALITY
- Show disclaimer: "AI-generated interpretation. Always confirm with your doctor or pharmacist before purchasing or substituting any medicine."
- Never claim a generic is clinically equivalent without matching composition and strength; warn when strength or form differs.
- Do not store or log uploaded images; process in memory.
- Add type hints, docstrings, and basic unit tests for the normalizer and matcher.

## BUILD ORDER (follow strictly; run and verify after each phase)
1. Scaffold backend + frontend, install dependencies, add `/api/health`, confirm both run and CORS works.
2. Create and clean datasets; implement normalizer + matcher with unit tests (test: "Dolo650", "Augmentin 625", "Pan-40" resolve correctly).
3. Implement Gemini vision service with structured output; test with a sample image and a mocked fallback response when no API key is set.
4. Wire `/api/analyze` end to end; verify JSON contract.
5. Build the frontend upload flow and results UI against the real API.
6. Add manual correction/search, editable fields, download/copy, error states.
7. Polish: loading UX, README with setup steps and architecture diagram, `.env.example`, a demo script, and sample prescription images in `/samples`.

## DELIVERABLES
Working app runnable with `uvicorn app.main:app --reload` and `npm run dev`, a README (setup, env vars, data sources, limitations), and a short list of known limitations and future improvements.

Start with Phase 1. After each phase, summarize what was built and what was tested before continuing.