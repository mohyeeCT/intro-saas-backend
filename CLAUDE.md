# intro-saas-backend — Repo Context

See `../CLAUDE.md` for full platform context, conventions, and working rules.

## What This Repo Is

FastAPI backend for the Intro Copy workflow.
Deployed on Railway EU West. Default branch: **`master`** (not main).
Current HEAD: `dd0d189`. Runtime: Python 3.12.

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
  dfs.py          — Keyword volume, difficulty, SERP
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
page_template: str = "standard"
word_count: int = 120
paragraph_count: int = 2
max_supporting_keywords: int = 3
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
