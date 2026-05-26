from pydantic import BaseModel
from typing import Optional


class JobRow(BaseModel):
    url: str
    keyword: Optional[str] = ""
    page_type: Optional[str] = "service_lp"
    h1: Optional[str] = ""


class JobSettings(BaseModel):
    # AI provider
    provider: str = "Claude"
    model: Optional[str] = None
    api_key: str

    # Copy config
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
    branded_terms_input: str = ""

    # DataForSEO
    dfs_login: str
    dfs_password: str
    location_code: int = 2840
    min_volume: int = 10

    # Scraping
    jina_api_key: str = ""
    scrape_pages: bool = False

    # GSC
    use_gsc: bool = True
    site_url: str = ""


class RunJobRequest(BaseModel):
    name: str
    rows: list[JobRow]
    settings: JobSettings
