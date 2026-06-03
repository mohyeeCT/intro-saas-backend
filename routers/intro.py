import re
import math
import time
import traceback
import urllib.parse
import requests
import base64
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from auth import get_current_user, get_supabase
from models import RunJobRequest, JobSettings, JobRow
from utils.copy_gen import generate_intro
from utils.dfs import get_keyword_overview, get_keyword_difficulty, _auth_header, DFS_BASE
from utils.gsc import get_gsc_client, get_top_queries_for_url
from utils.niches import get_niche_context
from utils.keyword import select_keyword
from utils.scraper import scrape_page_context

router = APIRouter()

_RATE_LIMITS = {
    "Claude": 0.5,
    "OpenAI": 0.5,
    "Gemini (free)": 5.0,
    "Mistral (free tier)": 2.0,
    "Groq (free tier)": 2.0,
}


# ── DFS ranked keywords for a specific page ───────────────────────────────────

def get_ranked_keywords_for_page(login: str, password: str, url: str, location_code: int = 2840) -> list:
    """Pull keywords the URL already ranks for via DFS dataforseo_labs/ranked_keywords.

    Takes domain (no scheme/www) and filters by relative path.
    Returns list of dicts: query, impressions (0), ctr (0), position, volume, difficulty.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lstrip("www.")
        relative_path = parsed.path or "/"
        if parsed.query:
            relative_path += "?" + parsed.query

        payload = [{
            "target": domain,
            "location_code": location_code,
            "language_code": "en",
            "limit": 100,
            "filters": [
                ["ranked_serp_element.serp_item.relative_url", "=", relative_path]
            ],
        }]

        r = requests.post(
            f"{DFS_BASE}/dataforseo_labs/google/ranked_keywords/live",
            headers=_auth_header(login, password),
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        results = []
        for task in data.get("tasks", []):
            for result_block in (task.get("result") or []):
                for item in (result_block.get("items") or []):
                    kw_data = item.get("keyword_data", {})
                    serp_el = item.get("ranked_serp_element", {}).get("serp_item", {})
                    kw = kw_data.get("keyword", "").strip().lower()
                    if not kw:
                        continue
                    volume = (kw_data.get("keyword_info", {}) or {}).get("search_volume", 0) or 0
                    difficulty = (kw_data.get("keyword_properties", {}) or {}).get("keyword_difficulty", 50) or 50
                    position = serp_el.get("rank_absolute", 99) or 99
                    results.append({
                        "query": kw,
                        "impressions": 0,
                        "ctr": 0.0,
                        "position": float(position),
                        "volume": volume,
                        "difficulty": difficulty,
                    })
        return results
    except Exception:
        return []


# ── Keyword pool merge and selection ─────────────────────────────────────────

def _stem(word: str) -> str:
    word = word.lower()
    for suffix in ("ing", "tion", "ed", "er", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _relevance_score(query: str, h1: str) -> float:
    if not h1:
        return 1.0
    q_stems = {_stem(w) for w in re.findall(r"[a-z]+", query.lower())}
    h_stems = {_stem(w) for w in re.findall(r"[a-z]+", h1.lower())}
    overlap = len(q_stems & h_stems)
    ratio = overlap / max(len(q_stems), 1)
    return round(min(1.5, 0.5 + ratio), 3)


def _position_score(position: float) -> float:
    """Positions 1-20 score 1.0. Beyond 20 drops off."""
    return 1 / (1 + max(0, position - 20) * 0.1)


def _score_candidate(
    kw: str, impressions: float, ctr: float, position: float,
    volume: int, difficulty: int, h1: str,
    restricted_industry: bool = False,
    clicks: float = 0,
) -> float:
    """Unified scoring for any keyword source.
    - CTR capped at 0.15 to prevent outlier CTR from dominating
    - restricted_industry: score on GSC signals only when DFS suppresses volume
    - Zero-volume: proxy score with 0.1 penalty in standard mode
    """
    difficulty = difficulty or 50
    pos_score = _position_score(position)
    rel_score = _relevance_score(kw, h1)
    ctr_capped = min(ctr, 0.15)
    ctr_boost = 1 + ctr_capped

    if volume == 0:
        if impressions > 0:
            if restricted_industry:
                clicks_boost = max(math.log1p(clicks), 1.0)
                return round(math.log1p(impressions) * clicks_boost * ctr_boost * pos_score * rel_score, 4)
            else:
                return round(math.log1p(impressions) * ctr_boost * 0.1, 4)
        return 0.0

    if restricted_industry:
        clicks_boost = max(math.log1p(clicks), 1.0)
        return round(math.log1p(impressions) * clicks_boost * ctr_boost * pos_score * rel_score, 4)

    return round((volume / difficulty) * math.log1p(impressions) * ctr_boost * pos_score * rel_score, 4)


def select_intro_keywords(
    gsc_queries: list,
    dfs_ranked: list,
    manual_seeds: list,
    dfs_volume_data: dict,
    dfs_diff_data: dict,
    branded_terms: list,
    min_volume: int,
    h1: str,
    max_supporting: int,
    used_primaries: set,
    restricted_industry: bool = False,
) -> dict:
    """Merge three keyword sources, score all, return primary + supporting.

    Sources:
      1. gsc_queries   -- {query, impressions, ctr, position}
      2. dfs_ranked    -- {query, impressions, ctr, position, volume, difficulty} (pre-enriched)
      3. manual_seeds  -- plain strings, added to pool at base score

    dfs_volume_data and dfs_diff_data are used to enrich gsc_queries.
    """

    def is_branded(kw: str) -> bool:
        kw_lower = kw.lower()
        return any(b.lower() in kw_lower for b in branded_terms if b)

    seen = {}  # dedup by keyword (lowercase)

    # 1. GSC queries enriched with DFS
    for q in gsc_queries:
        kw = q["query"].lower()
        if is_branded(kw) or q.get("position", 99) == 1.0:
            continue
        vol = dfs_volume_data.get(kw, {}).get("volume", 0) or 0
        diff = dfs_diff_data.get(kw, {}).get("difficulty", 50) or 50
        impressions = q.get("impressions", 0)
        if not restricted_industry and vol < min_volume and impressions == 0:
            continue
        sc = _score_candidate(kw, impressions, q.get("ctr", 0), q.get("position", 99), vol, diff, h1,
                              restricted_industry=restricted_industry, clicks=q.get("clicks", 0))
        if sc > 0 and (kw not in seen or sc > seen[kw]["score"]):
            seen[kw] = {"keyword": q["query"], "score": sc, "volume": vol, "difficulty": diff, "source": "gsc"}

    # 2. DFS ranked keywords (already have volume/difficulty embedded)
    for r in dfs_ranked:
        kw = r["query"].lower()
        if is_branded(kw) or r.get("position", 99) == 1.0:
            continue
        vol = r.get("volume", 0) or 0
        diff = r.get("difficulty", 50) or 50
        impressions = r.get("impressions", 0)
        if not restricted_industry and vol < min_volume and impressions == 0:
            continue
        sc = _score_candidate(kw, impressions, r.get("ctr", 0), r.get("position", 99), vol, diff, h1,
                              restricted_industry=restricted_industry, clicks=r.get("clicks", 0))
        if sc > 0 and (kw not in seen or sc > seen[kw]["score"]):
            seen[kw] = {"keyword": r["query"], "score": sc, "volume": vol, "difficulty": diff, "source": "dfs_ranked"}

    # 3. Manual seeds -- add to pool with DFS volume if available, else base score 0.01
    for seed in manual_seeds:
        kw = seed.lower().strip()
        if not kw or is_branded(kw):
            continue
        vol = dfs_volume_data.get(kw, {}).get("volume", 0) or 0
        diff = dfs_diff_data.get(kw, {}).get("difficulty", 50) or 50
        sc = _score_candidate(kw, 0, 0, 50, vol, diff, h1) if vol >= min_volume else 0.01
        if kw not in seen:
            seen[kw] = {"keyword": seed, "score": sc, "volume": vol, "difficulty": diff, "source": "manual"}

    if not seen:
        return {"primary": None, "supporting": [], "cluster_source": "none", "runner_up": None}

    ranked = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    # Deduplicate primary across job run
    primary = None
    runner_up = None
    for candidate in ranked:
        if candidate["keyword"].lower() not in used_primaries:
            primary = candidate
            break
    if primary is None:
        primary = ranked[0]  # all used -- fall back to top

    # Runner-up: next unused after primary
    for candidate in ranked:
        if candidate is not primary and candidate["keyword"].lower() != primary["keyword"].lower():
            runner_up = candidate
            break

    # Supporting: top N after primary, excluding primary
    supporting = [
        c for c in ranked
        if c["keyword"].lower() != primary["keyword"].lower()
    ][:max_supporting]

    # Determine cluster source
    sources = {c["source"] for c in [primary] + supporting}
    if "gsc" in sources and "dfs_ranked" in sources:
        cluster_source = "gsc+dfs"
    elif "gsc" in sources:
        cluster_source = "gsc"
    elif "dfs_ranked" in sources:
        cluster_source = "dfs_ranked"
    elif "manual" in sources:
        cluster_source = "manual"
    else:
        cluster_source = "manual"

    return {
        "primary": primary,
        "supporting": supporting,
        "cluster_source": cluster_source,
        "runner_up": runner_up,
        "primary_volume": primary.get("volume", 0),
        "primary_difficulty": primary.get("difficulty", 50),
    }


# ── H1 fallback keyword extraction ───────────────────────────────────────────

def h1_phrase_seeds(h1: str) -> list:
    """Extract candidate phrase seeds from H1 for DFS volume lookup."""
    if not h1:
        return []
    words = re.findall(r"[a-zA-Z0-9]+", h1.lower())
    seeds = []
    # Full H1 phrase
    full = " ".join(words)
    if full:
        seeds.append(full)
    # Bigrams and trigrams
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            seeds.append(" ".join(words[i:i + n]))
    return list(dict.fromkeys(seeds))[:10]  # dedupe, cap at 10


# ── Empty result template ─────────────────────────────────────────────────────

def _empty_result(url: str, status: str = "error", error: str = None) -> dict:
    return {
        "url": url,
        "intro_copy": "",
        "primary_keyword": "",
        "supporting_keywords": "",
        "word_count": 0,
        "cluster_source": "",
        "keyword_source": status,
        "scrape_status": "skipped",
        "runner_up": "",
        "primary_volume": 0,
        "primary_difficulty": 0,
        "status": status,
        "error": error,
    }


# ── Single row processor ──────────────────────────────────────────────────────

def _process_single_row(
    row: dict,
    settings: dict,
    gsc_client,
    branded_terms: list,
    used_primaries: set,
    sb=None,
    job_id: str = None,
    row_num: int = 1,
    total_rows: int = 1,
    brand_profile: dict = None,
) -> dict:
    def step(msg):
        if sb and job_id:
            _update_job(sb, job_id, {"current_step": f"Row {row_num}/{total_rows}: {msg}"})

    url = (row.get("url") or "").strip()
    h1_raw = (row.get("h1") or "").strip()
    h1 = "" if h1_raw.lower() in ("none", "None") else h1_raw
    manual_keyword_raw = (row.get("keyword") or "").strip()
    manual_seeds = [k.strip() for k in manual_keyword_raw.split(",") if k.strip()]

    # 0. Validate URL
    if not url or not url.startswith("http"):
        return _empty_result(url, status="skipped: invalid URL")

    # 1. Scrape page (optional)
    step("scraping page...")
    page_context = ""
    scrape_status = "skipped"
    if settings.get("scrape_pages") and settings.get("jina_api_key"):
        scrape_result = scrape_page_context(settings["jina_api_key"], url)
        if scrape_result.get("success"):
            page_context = scrape_result["content"]
            scrape_status = f"ok ({len(page_context)} chars)"
            step(f"scrape ok — {len(page_context):,} chars extracted")
        else:
            scrape_status = f"failed: {scrape_result.get('error', 'unknown')[:80]}"
            step(f"⚠ scrape failed — {scrape_result.get('error', 'unknown')[:60]}")
    else:
        step("scrape skipped (disabled or no Jina key)")

    # Append niche context to page_context so it reaches the AI prompt
    _niche_ctx = get_niche_context(settings.get("niche", ""))
    if _niche_ctx:
        page_context = (page_context + "\n\n" + _niche_ctx).strip()

    # 2. Collect keyword sources
    gsc_queries = []
    dfs_ranked = []
    keyword_source_label = ""

    # GSC
    if gsc_client and settings.get("site_url"):
        step("fetching GSC queries...")
        gsc_result = get_top_queries_for_url(gsc_client, settings["site_url"], url, top_n=10)
        if gsc_result and not gsc_result[0].get("_error"):
            gsc_queries = gsc_result
            step(f"GSC: {len(gsc_queries)} quer" + ("y" if len(gsc_queries)==1 else "ies") + " found")
        elif gsc_result and gsc_result[0].get("_error"):
            keyword_source_label = f"GSC error: {gsc_result[0]['_error'][:100]}"
            step(f"✗ GSC error — {gsc_result[0]['_error'][:80]}")

    # DFS ranked keywords for this page
    step("fetching DFS ranked keywords...")
    dfs_ranked = get_ranked_keywords_for_page(
        settings["dfs_login"], settings["dfs_password"],
        url, settings.get("location_code", 2840),
    )
    if dfs_ranked:
        step(f"DFS ranked keywords: {len(dfs_ranked)} found")

    # 3. DFS volume + difficulty for all unique query strings
    all_query_strings = list({q["query"].lower() for q in gsc_queries})
    # Also add manual seeds to lookup pool
    for seed in manual_seeds:
        kw_lower = seed.lower()
        if kw_lower and kw_lower not in all_query_strings:
            all_query_strings.append(kw_lower)

    dfs_volume_data = {}
    dfs_diff_data = {}

    if all_query_strings:
        step("fetching keyword volume + difficulty...")
        vol_raw = get_keyword_overview(
            settings["dfs_login"], settings["dfs_password"],
            all_query_strings, settings.get("location_code", 2840),
        )
        diff_raw = get_keyword_difficulty(
            settings["dfs_login"], settings["dfs_password"],
            all_query_strings, settings.get("location_code", 2840),
        )
        dfs_volume_data = vol_raw
        dfs_diff_data = diff_raw

    # 4. Select keywords from merged pool
    step("scoring keyword pool...")
    selection = select_intro_keywords(
        gsc_queries=gsc_queries,
        dfs_ranked=dfs_ranked,
        manual_seeds=manual_seeds,
        dfs_volume_data=dfs_volume_data,
        dfs_diff_data=dfs_diff_data,
        branded_terms=branded_terms,
        min_volume=settings.get("min_volume", 10),
        h1=h1,
        max_supporting=settings.get("max_supporting_keywords", 5),
        used_primaries=used_primaries,
        restricted_industry=settings.get("restricted_industry", False),
    )

    primary_kw_data = selection.get("primary")
    cluster_source = selection.get("cluster_source", "none")
    runner_up_data = selection.get("runner_up")

    if primary_kw_data:
        _kw = primary_kw_data.get("keyword", "")
        _vol = primary_kw_data.get("volume") or 0
        _src = cluster_source or "unknown"
        step("keyword selected: " + str(_kw) + " [" + str(_src) + "]" + (", vol:" + str(_vol) if _vol else ""))
    else:
        step("⚠ no keyword found from GSC/DFS/manual — checking H1 fallback...")

    # 5. H1 fallback if no keyword found
    if not primary_kw_data and h1:
        step("no keyword found — running H1 fallback...")
        seeds = h1_phrase_seeds(h1)
        if seeds:
            h1_vol = get_keyword_overview(
                settings["dfs_login"], settings["dfs_password"],
                seeds, settings.get("location_code", 2840),
            )
            # Pick seed with most volume
            best_seed = None
            best_vol = 0
            for seed in seeds:
                vol = h1_vol.get(seed.lower(), {}).get("volume", 0) or 0
                if vol > best_vol:
                    best_vol = vol
                    best_seed = seed
            if best_seed and best_vol >= settings.get("min_volume", 10):
                h1_diff_data = get_keyword_difficulty(
                    settings["dfs_login"], settings["dfs_password"],
                    [best_seed], settings.get("location_code", 2840),
                )
                primary_kw_data = {
                    "keyword": best_seed,
                    "score": 0.0,
                    "volume": best_vol,
                    "difficulty": h1_diff_data.get(best_seed.lower(), {}).get("difficulty", 50) or 50,
                    "source": "h1_fallback",
                }
                cluster_source = "h1_fallback"
            else:
                # Volume too low but use H1 as literal fallback keyword
                primary_kw_data = {
                    "keyword": h1,
                    "score": 0.0,
                    "volume": 0,
                    "difficulty": 50,
                    "source": "h1_fallback",
                }
                cluster_source = "h1_fallback"

    if not primary_kw_data:
        step("✗ no keyword found after all sources — skipping AI call")
        return {
            **_empty_result(url, status="skipped: no keyword found"),
            "cluster_source": cluster_source,
            "scrape_status": scrape_status,
        }

    primary_keyword = primary_kw_data["keyword"]
    supporting_kws = [c["keyword"] for c in (selection.get("supporting") or [])]
    runner_up = runner_up_data["keyword"] if runner_up_data else ""

    # Build keyword_source label
    if not keyword_source_label:
        has_gsc = bool(gsc_queries)
        has_dfs = bool(dfs_ranked)
        has_manual = bool(manual_seeds)
        if cluster_source == "h1_fallback":
            keyword_source_label = "h1_fallback"
        elif has_gsc and has_dfs:
            keyword_source_label = "gsc+dfs"
        elif has_gsc:
            keyword_source_label = "gsc"
        elif has_dfs:
            keyword_source_label = "dfs_ranked"
        elif has_manual:
            keyword_source_label = "manual"
        else:
            keyword_source_label = "fallback"

    # 6. Generate intro copy
    step(f"generating intro with {settings.get('provider', 'AI')}...")
    try:
        intro_copy = generate_intro(
            provider=settings["provider"],
            api_key=settings["api_key"],
            primary_keyword=primary_keyword,
            supporting_keywords=supporting_kws,
            page_template=settings.get("page_template", "service_lp"),
            business_type=settings.get("business_type", "general"),
            brand_name=settings.get("brand_name", ""),
            include_brand=settings.get("include_brand", False),
            h1=h1,
            word_count=settings.get("word_count", 100),
            paragraph_count=settings.get("paragraph_count", 1),
            page_context=page_context,
            forbidden_phrases="\n".join(
                p.strip() for p in settings.get("forbidden_phrases", "").strip().splitlines() if p.strip()
            ),
            model=settings.get("model"),
            brand_profile=brand_profile or {},
        )
    except Exception as e:
        return {
            **_empty_result(url, status="error", error=str(e)),
            "primary_keyword": primary_keyword,
            "supporting_keywords": ", ".join(supporting_kws),
            "cluster_source": cluster_source,
            "keyword_source": keyword_source_label,
            "scrape_status": scrape_status,
            "runner_up": runner_up,
            "primary_volume": primary_kw_data.get("volume", 0),
            "primary_difficulty": primary_kw_data.get("difficulty", 50),
        }

    actual_word_count = len(intro_copy.split())
    step(f"✓ intro generated — {actual_word_count} words")

    # Track primary so next rows skip it
    used_primaries.add(primary_keyword.lower())

    return {
        "url": url,
        "intro_copy": intro_copy,
        "primary_keyword": primary_keyword,
        "supporting_keywords": ", ".join(supporting_kws),
        "word_count": actual_word_count,
        "cluster_source": cluster_source,
        "keyword_source": keyword_source_label,
        "scrape_status": scrape_status,
        "runner_up": runner_up,
        "primary_volume": primary_kw_data.get("volume", 0),
        "primary_difficulty": primary_kw_data.get("difficulty", 50),
        "status": "ok",
        "error": None,
    }


# ── Background job processor ──────────────────────────────────────────────────

def _is_cancelled(sb, job_id: str) -> bool:
    """Check if job has been cancelled by the user."""
    try:
        res = sb.table("jobs").select("status").eq("id", job_id).execute()
        return res.data and res.data[0].get("status") == "cancelling"
    except Exception:
        return False


def _process_job(job_id: str, rows: list, settings: dict, sa_info: dict, brand_profile: dict = None):
    sb = get_supabase()
    results = []
    used_primaries = set()
    delay = _RATE_LIMITS.get(settings["provider"], 1.0)

    gsc_client = None
    if settings.get("use_gsc") and sa_info:
        try:
            gsc_client = get_gsc_client(sa_info)
        except Exception as e:
            _update_job(sb, job_id, {"error": f"GSC auth failed: {e}"})

    branded_terms = [b.strip() for b in settings.get("brand_name", "").split() if b.strip()]
    full_brand_name = settings.get("full_brand_name", "").strip()
    if full_brand_name:
        import re as _re
        full_name_words = [w.lower() for w in _re.findall(r"[a-zA-Z]+", full_brand_name) if len(w) >= 3]
        branded_terms = list(set(branded_terms + full_name_words))
    branded_terms_input = settings.get("branded_terms_input", "").strip()
    if branded_terms_input:
        manual_terms = [t.strip().lower() for t in branded_terms_input.splitlines() if t.strip()]
        branded_terms = list(set(branded_terms + manual_terms))

    total = len(rows)
    for idx, row in enumerate(rows):
        url = row.get("url", "")
        try:
            _update_job(sb, job_id, {"current_step": f"Row {idx+1}/{total}: starting — {url}"})
            result = _process_single_row(
                row=row,
                settings=settings,
                gsc_client=gsc_client,
                branded_terms=branded_terms,
                used_primaries=used_primaries,
                sb=sb,
                job_id=job_id,
                row_num=idx + 1,
                total_rows=total,
                brand_profile=brand_profile,
            )
            results.append(result)
        except Exception:
            results.append({
                **_empty_result(url, status="error", error=traceback.format_exc(limit=3)),
                "scrape_status": "error",
            })

        _update_job(sb, job_id, {
            "completed_rows": idx + 1,
            "results": results,
        })

        if _is_cancelled(sb, job_id):
            _update_job(sb, job_id, {
                "status": "cancelled",
                "current_step": f"Cancelled after {idx + 1}/{total} rows.",
                "failed_rows": sum(1 for r in results if r.get("error") or r.get("status") == "error"),
            })
            return

        if idx < len(rows) - 1:
            time.sleep(delay)

    if _is_cancelled(sb, job_id):
        _update_job(sb, job_id, {
            "status": "cancelled",
            "current_step": "Cancelled.",
            "failed_rows": sum(1 for r in results if r.get("error") or r.get("status") == "error"),
            "results": results,
        })
        return

    _update_job(sb, job_id, {
        "status": "complete",
        "current_step": "Done.",
        "completed_rows": len(results),
        "failed_rows": sum(1 for r in results if r.get("error") or r.get("status") == "error"),
        "results": results,
    })


def _update_job(sb, job_id: str, data: dict):
    try:
        update_data = {**data, "updated_at": "now()"}
        if "current_step" in data and data["current_step"]:
            from datetime import datetime, timezone
            log_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "msg": data["current_step"],
            }
            try:
                res = sb.table("jobs").select("logs").eq("id", job_id).execute()
                current_logs = (res.data[0].get("logs") or []) if res.data else []
                current_logs.append(log_entry)
                update_data["logs"] = current_logs
            except Exception:
                pass
        sb.table("jobs").update(update_data).eq("id", job_id).execute()
    except Exception:
        pass


# ── Route handlers ────────────────────────────────────────────────────────────

@router.post("/run")
def run_intro_job(
    request: RunJobRequest,
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user),
):
    sb = get_supabase()

    sa_info = None
    brand_profile = {}
    try:
        res = sb.table("user_settings").select("gsc_service_account").eq("user_id", user.id).execute()
        if res.data and request.settings.use_gsc:
            sa_info = res.data[0].get("gsc_service_account")
    except Exception:
        pass

    # Fetch brand profile by ID if provided
    brand_profile_id = request.settings.model_dump().get("brand_profile_id")
    if brand_profile_id:
        try:
            bp_res = sb.table("brand_profiles").select("data").eq("id", brand_profile_id).eq("user_id", user.id).execute()
            if bp_res.data:
                brand_profile = bp_res.data[0].get("data") or {}
        except Exception:
            pass

    job_data = {
        "user_id": user.id,
        "status": "running",
        "name": request.name,
        "tool": "intro",
        "settings": request.settings.model_dump(exclude={"api_key", "dfs_password"}),
        "rows": [r.model_dump() for r in request.rows],
        "results": [],
        "total_rows": len(request.rows),
        "completed_rows": 0,
        "current_step": "Starting...",
    }

    res = sb.table("jobs").insert(job_data).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create job")

    job_id = res.data[0]["id"]

    background_tasks.add_task(
        _process_job,
        job_id=job_id,
        rows=[r.model_dump() for r in request.rows],
        settings=request.settings.model_dump(),
        sa_info=sa_info,
        brand_profile=brand_profile,
    )

    return {"job_id": job_id, "status": "running"}
