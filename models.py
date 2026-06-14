from pydantic import BaseModel, field_validator
from typing import Optional

WORD_COUNT_MIN, WORD_COUNT_MAX = 60, 300
PARAGRAPH_COUNT_MIN, PARAGRAPH_COUNT_MAX = 1, 5


def _clamp_int(value, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


class JobRow(BaseModel):
    url: str
    keyword: Optional[str] = ""
    page_type: Optional[str] = "service_lp"
    h1: Optional[str] = ""


class JobSettings(BaseModel):
    # AI provider
    provider: str = "Claude"
    model: Optional[str] = None
    api_key: str = ""

    # Copy config
    niche: str = "none"
    business_type: str = "general"
    page_template: str = "service_lp"
    word_count: int = 100
    paragraph_count: int = 1
    max_supporting_keywords: int = 5
    brand_name: str = ""
    full_brand_name: str = ""
    brand_profile_id: str = ""  # ID from brand_profiles table
    include_brand: bool = False
    forbidden_phrases: str = ""
    restricted_industry: bool = False  # Score on GSC signals only when DFS suppresses volume
    branded_terms_input: str = ""

    # DataForSEO
    dfs_login: str = ""
    dfs_password: str = ""
    location_code: int = 2840
    min_volume: int = 10

    # Scraping
    jina_api_key: str = ""
    scrape_pages: bool = False

    # GSC
    use_gsc: bool = True
    site_url: str = ""

    @field_validator("word_count", mode="before")
    @classmethod
    def clamp_word_count(cls, value):
        return _clamp_int(value, minimum=WORD_COUNT_MIN, maximum=WORD_COUNT_MAX, default=100)

    @field_validator("paragraph_count", mode="before")
    @classmethod
    def clamp_paragraph_count(cls, value):
        return _clamp_int(value, minimum=PARAGRAPH_COUNT_MIN, maximum=PARAGRAPH_COUNT_MAX, default=1)


class RunJobRequest(BaseModel):
    name: str
    rows: list[JobRow]
    settings: JobSettings
