from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from auth import get_current_user, get_supabase

router = APIRouter()


from pydantic import BaseModel

class RenameRequest(BaseModel):
    name: str


@router.patch("/{job_id}/rename")
def rename_job(job_id: str, body: RenameRequest, user=Depends(get_current_user)):
    sb = get_supabase()
    res = (
        sb.table("jobs")
        .update({"name": body.name.strip()})
        .eq("id", job_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"renamed": True}


@router.get("")
def list_jobs(user=Depends(get_current_user)):
    """Return all jobs for the current user, newest first."""
    sb = get_supabase()
    res = (
        sb.table("jobs")
        .select("id, name, status, total_rows, completed_rows, failed_rows, created_at, updated_at, error")
        .eq("user_id", user.id)
        .eq("tool", "intro")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return res.data or []


@router.get("/{job_id}")
def get_job(job_id: str, user=Depends(get_current_user)):
    """Return full job including results."""
    sb = get_supabase()
    res = (
        sb.table("jobs")
        .select("*")
        .eq("id", job_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")
    return res.data[0]


@router.delete("/{job_id}")
def delete_job(job_id: str, user=Depends(get_current_user)):
    """Delete a job from history."""
    sb = get_supabase()
    res = (
        sb.table("jobs")
        .delete()
        .eq("id", job_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"deleted": True}


class RerunRequest(BaseModel):
    keyword_override: str = ""


@router.post("/{job_id}/rerun-row/{row_index}")
def rerun_row(
    job_id: str,
    row_index: int,
    body: RerunRequest = None,
    background_tasks: BackgroundTasks = None,
    user=Depends(get_current_user),
    sb=Depends(get_supabase),
):
    """Re-run a single row in a completed job, optionally with a keyword override."""
    res = sb.table("jobs").select("*").eq("id", job_id).eq("user_id", user.id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    job = res.data[0]
    rows = job.get("rows", [])
    settings = job.get("settings", {})

    if row_index < 0 or row_index >= len(rows):
        raise HTTPException(status_code=400, detail="Row index out of range")

    keyword_override = (body.keyword_override or "").strip() if body else ""

    step_msg = f"Re-running row {row_index + 1}"
    if keyword_override:
        step_msg += f' with keyword "{keyword_override}"'
    step_msg += "..."

    sb.table("jobs").update({
        "current_step": step_msg,
        "updated_at": "now()"
    }).eq("id", job_id).execute()

    background_tasks.add_task(_rerun_single_row, job_id, row_index, rows, settings, sb, keyword_override)
    return {"status": "rerunning"}


def _rerun_single_row(job_id: str, row_index: int, rows: list, settings: dict, sb, keyword_override: str = ""):
    """Background task to re-run one row and update its result in place."""
    import traceback, time
    from utils.copy_gen import generate_intro
    from utils.dfs import get_keyword_overview, get_keyword_difficulty
    from utils.gsc import get_gsc_client, get_top_queries_for_url
    from utils.scraper import scrape_page_context

    try:
        row = rows[row_index]
        # Apply keyword override if provided - inject as manual keyword
        if keyword_override:
            row = {**row, "keyword": keyword_override}
        api_key = settings.get("api_key", "")

        # Re-init GSC if needed
        gsc_client = None
        if settings.get("use_gsc"):
            try:
                sa_res = sb.table("user_settings").select("gsc_service_account").limit(1).execute()
                if sa_res.data and sa_res.data[0].get("gsc_service_account"):
                    gsc_client = get_gsc_client(sa_res.data[0]["gsc_service_account"])
            except Exception:
                pass

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

        # Temporarily inject api_key (not stored in settings for security)
        settings_with_key = {**settings, "api_key": api_key}

        # Run the single row through full pipeline
        from routers.intro import _process_single_row, _update_job
        result = _process_single_row(
            row=row,
            settings=settings_with_key,
            gsc_client=gsc_client,
            branded_terms=branded_terms,
            used_keywords=set(),
            used_question_patterns=[],
            sb=sb,
            job_id=job_id,
            row_num=row_index + 1,
            total_rows=len(rows),
        )

        # Update just this row's result in the existing results array
        res = sb.table("jobs").select("results").eq("id", job_id).execute()
        current_results = res.data[0].get("results", []) if res.data else []

        # Extend if needed
        while len(current_results) <= row_index:
            current_results.append({})
        current_results[row_index] = result

        sb.table("jobs").update({
            "results": current_results,
            "current_step": f"Row {row_index + 1} complete.",
            "updated_at": "now()"
        }).eq("id", job_id).execute()

    except Exception:
        sb.table("jobs").update({
            "current_step": f"Row {row_index + 1} failed: {traceback.format_exc(limit=1)[:120]}",
            "updated_at": "now()"
        }).eq("id", job_id).execute()


@router.post("/{job_id}/duplicate")
def duplicate_job(
    job_id: str,
    user=Depends(get_current_user),
    sb=Depends(get_supabase),
):
    """Duplicate a job's settings and rows as a new draft job."""
    res = sb.table("jobs").select("*").eq("id", job_id).eq("user_id", user.id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    original = res.data[0]

    new_job = {
        "user_id": user.id,
        "status": "draft",
        "name": f"{original.get('name', 'Job')} (copy)",
        "settings": original.get("settings", {}),
        "rows": original.get("rows", []),
        "results": [],
        "total_rows": original.get("total_rows", 0),
        "completed_rows": 0,
        "current_step": "",
    }

    new_res = sb.table("jobs").insert(new_job).execute()
    if not new_res.data:
        raise HTTPException(status_code=500, detail="Failed to duplicate job")

    return {"job_id": new_res.data[0]["id"]}
