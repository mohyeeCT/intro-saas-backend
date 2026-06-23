import unittest
import urllib.parse
import sys
import types
from unittest.mock import Mock, patch


def _install_router_import_stubs():
    requests_stub = types.ModuleType("requests")
    requests_stub.post = Mock()
    sys.modules.setdefault("requests", requests_stub)

    fastapi_stub = types.ModuleType("fastapi")

    class _Router:
        def post(self, *args, **kwargs):
            return lambda fn: fn

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def delete(self, *args, **kwargs):
            return lambda fn: fn

    fastapi_stub.APIRouter = lambda *args, **kwargs: _Router()
    fastapi_stub.BackgroundTasks = object
    fastapi_stub.Depends = lambda *args, **kwargs: None
    fastapi_stub.HTTPException = Exception
    sys.modules.setdefault("fastapi", fastapi_stub)

    auth_stub = types.ModuleType("auth")
    auth_stub.get_current_user = lambda: None
    auth_stub.get_supabase = lambda: None
    sys.modules.setdefault("auth", auth_stub)

    models_stub = types.ModuleType("models")
    models_stub.RunJobRequest = object
    models_stub.JobSettings = object
    models_stub.JobRow = object
    sys.modules.setdefault("models", models_stub)

    gsc_stub = types.ModuleType("utils.gsc")
    gsc_stub.get_gsc_client = lambda *args, **kwargs: None
    gsc_stub.get_top_queries_for_url = lambda *args, **kwargs: []
    sys.modules.setdefault("utils.gsc", gsc_stub)

    niches_stub = types.ModuleType("utils.niches")
    niches_stub.get_niche_context = lambda *args, **kwargs: ""
    sys.modules.setdefault("utils.niches", niches_stub)

    keyword_stub = types.ModuleType("utils.keyword")
    keyword_stub.select_keyword = lambda *args, **kwargs: None
    sys.modules.setdefault("utils.keyword", keyword_stub)

    scraper_stub = types.ModuleType("utils.scraper")
    scraper_stub.scrape_page_context = lambda *args, **kwargs: {"success": False}
    scraper_stub.is_ecommerce_collection_page = lambda *args, **kwargs: False
    sys.modules.setdefault("utils.scraper", scraper_stub)


_install_router_import_stubs()

from routers.intro import _relative_url_variants, get_ranked_keywords_for_page
from utils.copy_gen import DEFAULT_MODELS
from utils.copy_gen import _build_prompt


class IntroOpenAIModelTests(unittest.TestCase):
    def test_openai_default_uses_current_gpt_5_model(self):
        self.assertEqual(DEFAULT_MODELS["OpenAI"], "gpt-5.5")
        self.assertNotEqual(DEFAULT_MODELS["OpenAI"], "gpt-4o-mini")

    def test_openai_gpt5_uses_max_completion_tokens(self):
        from utils import copy_gen

        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(message=types.SimpleNamespace(content="Intro copy"))
                    ]
                )

        class FakeClient:
            def __init__(self, api_key):
                self.chat = types.SimpleNamespace(completions=FakeCompletions())

        openai_stub = types.ModuleType("openai")
        openai_stub.OpenAI = FakeClient
        original_openai = sys.modules.get("openai")
        sys.modules["openai"] = openai_stub
        try:
            copy_gen._call_openai("key", "prompt", max_tokens=123, model="gpt-5.5")
        finally:
            if original_openai is None:
                sys.modules.pop("openai", None)
            else:
                sys.modules["openai"] = original_openai

        self.assertEqual(captured["model"], "gpt-5.5")
        self.assertEqual(captured["max_completion_tokens"], 123)
        self.assertNotIn("max_tokens", captured)


class IntroPromptGuardrailTests(unittest.TestCase):
    def test_prompt_includes_unsupported_claim_guardrails(self):
        prompt = _build_prompt(
            primary_keyword="running shoes",
            supporting_keywords=["trail running shoes"],
            page_template="category",
            business_type="ecommerce",
            brand_name="Example",
            include_brand=False,
            h1="Running Shoes",
            word_count=80,
            paragraph_count=1,
            page_context="Products from $49 with many sizes in stock.",
            forbidden_phrases="",
            brand_profile={},
        )

        self.assertIn("UNSUPPORTED CLAIM GUARDRAIL", prompt)
        self.assertIn("pricing", prompt)
        self.assertIn("availability", prompt)
        self.assertIn("strategy signals only", prompt)
        self.assertIn("SCRAPED CONTEXT GUARDRAIL", prompt)
        self.assertIn("stock levels", prompt)


class RankedKeywordUrlVariantTests(unittest.TestCase):
    def test_relative_url_variants_try_trailing_and_non_trailing_slash(self):
        parsed = urllib.parse.urlparse("https://example.com/products/widgets/")

        self.assertEqual(
            _relative_url_variants(parsed),
            ["/products/widgets/", "/products/widgets"],
        )

    @patch("routers.intro.requests.post")
    def test_ranked_keywords_returns_results_from_second_url_variant(self, mock_post):
        empty_response = Mock()
        empty_response.raise_for_status.return_value = None
        empty_response.json.return_value = {"tasks": [{"result": [{"items": []}]}]}

        result_response = Mock()
        result_response.raise_for_status.return_value = None
        result_response.json.return_value = {
            "tasks": [{
                "result": [{
                    "items": [{
                        "keyword_data": {
                            "keyword": "running shoes",
                            "keyword_info": {"search_volume": 1200},
                            "keyword_properties": {"keyword_difficulty": 34},
                        },
                        "ranked_serp_element": {
                            "serp_item": {"rank_absolute": 8}
                        },
                    }]
                }]
            }]
        }
        mock_post.side_effect = [empty_response, result_response]

        results = get_ranked_keywords_for_page(
            "login",
            "password",
            "https://example.com/products/widgets/",
        )

        self.assertEqual(results[0]["query"], "running shoes")
        self.assertEqual(results[0]["volume"], 1200)
        requested_paths = [
            call.kwargs["json"][0]["filters"][0][2]
            for call in mock_post.call_args_list
        ]
        self.assertEqual(requested_paths, ["/products/widgets/", "/products/widgets"])


if __name__ == "__main__":
    unittest.main()
