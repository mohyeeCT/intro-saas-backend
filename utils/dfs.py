import requests
import base64

DFS_BASE = "https://api.dataforseo.com/v3"


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json"
    }


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    msg_lower = msg.lower()
    if "401" in msg:
        return "Invalid DataForSEO login or password."
    if "403" in msg:
        return "DataForSEO account lacks required API permissions."
    if "429" in msg or "20001" in msg or "too many requests" in msg_lower:
        return "DataForSEO rate limit reached. Wait 60 seconds and retry."
    if "timed out" in msg_lower or "timeout" in msg_lower:
        return "DataForSEO request timed out. Check your internet connection."
    if "connectionerror" in msg_lower or "remotedisconnected" in msg_lower:
        return "Cannot connect to DataForSEO API. Check your internet connection."
    if "40501" in msg:
        return "DataForSEO: Invalid parameters. Check location code and keyword format."
    return f"DataForSEO error: {msg}"


def _raise_api_error(data: dict) -> None:
    status_code = data.get("status_code")
    if status_code is not None and status_code != 20000:
        raise RuntimeError(f"{status_code} {data.get('status_message', 'Unknown API error')}")
    for task in data.get("tasks") or []:
        task_status = task.get("status_code")
        if task_status is not None and task_status != 20000:
            raise RuntimeError(f"{task_status} {task.get('status_message', 'Unknown task error')}")


def get_keyword_overview(login: str, password: str, keywords: list, location_code: int = 2840) -> dict:
    """Returns dict keyed by lowercase keyword: {volume, cpc, competition}."""
    if not keywords:
        return {}
    payload = [{"keywords": keywords, "location_code": location_code, "language_code": "en"}]
    try:
        r = requests.post(
            f"{DFS_BASE}/keywords_data/google_ads/search_volume/live",
            headers=_auth_header(login, password),
            json=payload,
            timeout=30
        )
        r.raise_for_status()
        data = r.json()
        _raise_api_error(data)
        result = {}
        for task in data.get("tasks", []):
            for item in (task.get("result") or []):
                kw = item.get("keyword", "").lower()
                result[kw] = {
                    "volume": item.get("search_volume", 0) or 0,
                    "cpc": item.get("cpc", 0),
                    "competition": item.get("competition", 0)
                }
        return result
    except Exception as e:
        raise RuntimeError(_friendly_error(e)) from e


def get_keyword_difficulty(login: str, password: str, keywords: list, location_code: int = 2840) -> dict:
    """Returns dict keyed by lowercase keyword: {difficulty}."""
    if not keywords:
        return {}
    payload = [{"keywords": keywords, "location_code": location_code, "language_code": "en"}]
    try:
        r = requests.post(
            f"{DFS_BASE}/dataforseo_labs/google/bulk_keyword_difficulty/live",
            headers=_auth_header(login, password),
            json=payload,
            timeout=30
        )
        r.raise_for_status()
        data = r.json()
        _raise_api_error(data)
        result = {}
        for task in data.get("tasks", []):
            for item in (task.get("result") or []):
                for kw_item in (item.get("items") or []):
                    kw = kw_item.get("keyword", "").lower()
                    kd = kw_item.get("keyword_difficulty")
                    result[kw] = {
                        "difficulty": kd if kd is not None else 50
                    }
        return result
    except Exception as e:
        raise RuntimeError(_friendly_error(e)) from e
