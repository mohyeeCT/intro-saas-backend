# intro-saas-backend — Repo Context

See `../CLAUDE.md` for full platform context, conventions, and working rules.

## What This Repo Is

FastAPI backend for the Intro Copy workflow.
Deployed on Railway EU West. Default branch: **`master`** (not main).
Current HEAD: `f701bad`. Runtime: Python 3.12.

Railway URL: `https://intro-saas-backend-production.up.railway.app`

## File Structure

```
main.py           — App, CORS, router mounts, global exception handler
auth.py           — Supabase token validation
models.py         — JobSettings, JobRow, JobRequest Pydantic models
routers/
  intro.py        — POST /api/intro/run + _process_single_row
  jobs.py         — Shared job CRUD
  settings.py     — Shared settings CRUD
utils/
  copy_gen.py     — generate_intro, sanitise, PROVIDER_FN, PROVIDER_DELAY
  dfs.py          — Keyword volume, difficulty (_friendly_error for human-readable errors)
  gsc.py          — GSC queries (checks both trailing and non-trailing URL variants)
  keyword.py      — select_keyword with used_primaries deduplication
  scraper.py      — Jina page scraping
  niches.py       — get_niche_context (23 niches)
schema.sql        — jobs, user_settings, brand_profiles tables
tests/
  test_cors.py
  test_dfs_error_visibility.py
```

## Endpoints

Same shared set as FAQ (see faq-saas-backend CLAUDE.md) with:
- `POST /api/intro/run` instead of `/api/faq/run`

## Intro Pipeline (_process_single_row)

1. Scrape page context via Jina (optional)
2. Inject niche context into page_context
3. Collect keyword sources: GSC (both trailing + non-trailing URL), DFS, manual seeds, H1 fallback
4. Select primary and supporting keywords using `used_primaries` deduplication set
5. Fetch LSI keywords (optional DFS call)
6. Generate intro copy: `generate_intro`
7. Apply safety guardrails (restricted industry, unsupported claims)
8. Write result to Supabase

## Key Model Fields (JobSettings)

```python
niche: str = "none"
business_type: str = "general"
provider: str = "Claude"
brand_name: str
full_brand_name: str = ""
include_brand: bool = True
forbidden_phrases: str = ""
brand_profile_id: str = ""
page_template: str = "service_lp"
word_count: int = 100          # clamped 60–300 via @field_validator
paragraph_count: int = 1       # clamped 1–5 via @field_validator
max_supporting_keywords: int = 5
```

## Known Gotchas

- Default branch is `master` — always specify `git checkout master` and
  `git push origin master`. Do not use `main`.
- GSC lookup checks both `https://example.com/page` and `https://example.com/page/`
  (trailing slash) because Google Search Console property format varies.
- `used_primaries` is a `set()` passed across all rows in a job run to prevent
  the same primary keyword being assigned to multiple rows.
- Safety guardrails reject copy containing restricted-industry language or
  unsupported absolute claims. Review `generate_intro` prompt rules before
  modifying prompt templates.
- `dfs.py` exports only `get_keyword_overview`, `get_keyword_difficulty`,
  `_auth_header`, `_raise_api_error`, and `DFS_BASE`. The old `get_serp_data`
  function and its helpers were removed — intro pipeline never called them.
- `routers/intro.py` does NOT import `select_keyword` from `utils/keyword.py`;
  it uses `select_intro_keywords` defined in the same file instead.
- `utils/scraper.py` exports `scrape_page_context` (with `mode` param) and
  `is_ecommerce_collection_page`. Ecommerce collection mode activates only when
  `business_type == "ecommerce"` AND `page_type` contains "category"/"collection".


## Local Dev Setup

Tests require FastAPI and all backend dependencies. Without a venv, `pytest`
will fail on collection with `ModuleNotFoundError: No module named 'fastapi'`.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

CI (GitHub Actions) installs dependencies automatically — this setup is only
needed for local test runs.
