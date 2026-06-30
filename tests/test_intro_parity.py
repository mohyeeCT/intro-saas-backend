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
    gsc_stub.GscOAuthConfigError = RuntimeError
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

import routers.intro as intro
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

    def test_sonnet_5_request_disables_thinking(self):
        from utils import copy_gen

        options = copy_gen._anthropic_request_options("claude-sonnet-5", 1000)

        self.assertEqual(options["thinking"], {"type": "disabled"})
        self.assertEqual(options["max_tokens"], 1000)

    def test_non_sonnet_5_request_leaves_thinking_unset(self):
        from utils import copy_gen

        options = copy_gen._anthropic_request_options("claude-sonnet-4-6", 1000)

        self.assertNotIn("thinking", options)

    def test_generate_intro_uses_expanded_token_limit(self):
        from utils import copy_gen

        captured = {}
        original_provider = copy_gen._PROVIDER_FN.get("Test")

        def fake_provider(api_key, prompt, max_tokens=1000, model=None):
            captured["max_tokens"] = max_tokens
            captured["model"] = model
            return "Generated intro copy."

        copy_gen._PROVIDER_FN["Test"] = fake_provider
        try:
            result = copy_gen.generate_intro(
                provider="Test",
                api_key="key",
                primary_keyword="SEO services",
                supporting_keywords=[],
                page_template="service_lp",
                business_type="service",
                brand_name="Example",
                include_brand=False,
                h1="SEO Services",
                word_count=80,
                paragraph_count=1,
                page_context="",
                forbidden_phrases="",
                model="test-intro-model",
            )
        finally:
            if original_provider is None:
                copy_gen._PROVIDER_FN.pop("Test", None)
            else:
                copy_gen._PROVIDER_FN["Test"] = original_provider

        self.assertEqual(result, "Generated intro copy.")
        self.assertEqual(captured["max_tokens"], 16384)
        self.assertEqual(captured["model"], "test-intro-model")


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

    def test_prompt_bans_this_page_output_phrasing(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=[],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="",
            forbidden_phrases="",
            brand_profile={},
        )

        self.assertIn("Do not write phrases like \"this page\", \"on this page\", or \"the page\"", prompt)
        self.assertIn("Refer directly to the service, category, product, topic, brand, or location instead", prompt)

    def test_prompt_blocks_common_generic_openers(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=[],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="",
            forbidden_phrases="",
            brand_profile={},
        )

        blocked_openers = [
            "Welcome to",
            "Are you looking for",
            "In today's world",
            "Whether you are",
            "Finding the right",
            "When it comes to",
            "Choosing the right",
            "Looking for",
            "There are many",
            "It can be difficult to",
            "If you are searching for",
            "Whether you need",
            "In the world of",
        ]
        for opener in blocked_openers:
            self.assertIn(opener, prompt)

    def test_prompt_requires_substantive_first_sentence(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=[],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="",
            forbidden_phrases="",
            brand_profile={},
        )

        self.assertIn("The first sentence must communicate the core topic, benefit, or value of the page", prompt)
        self.assertIn("why it matters to them after reading only the first sentence", prompt)
        self.assertIn("not for warming up or establishing context", prompt)

    def test_blog_and_brand_prompts_include_hook_structures(self):
        for template in ("blog", "brand"):
            prompt = _build_prompt(
                primary_keyword="SEO audit services",
                supporting_keywords=[],
                page_template=template,
                business_type="service",
                brand_name="Example",
                include_brand=False,
                h1="SEO Audit Services",
                word_count=80,
                paragraph_count=1,
                page_context="",
                forbidden_phrases="",
                brand_profile={},
            )

            self.assertIn("BLOG AND BRAND HOOK STRUCTURE", prompt)
            self.assertIn("Concrete outcome", prompt)
            self.assertIn("Specific fact", prompt)
            self.assertIn("Direct assertion", prompt)
            self.assertIn("Problem frame", prompt)
            self.assertIn("Make the chosen hook the first sentence", prompt)
            self.assertIn("not preceded by a wind-up sentence", prompt)

    def test_non_editorial_prompts_do_not_include_hook_structures(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=[],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="",
            forbidden_phrases="",
            brand_profile={},
        )

        self.assertNotIn("BLOG AND BRAND HOOK STRUCTURE", prompt)
        self.assertNotIn("Concrete outcome", prompt)

    def test_prompt_allows_natural_keyword_variation_in_opening_paragraph(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=["technical SEO audit"],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="",
            forbidden_phrases="",
            brand_profile={},
        )

        self.assertIn("Represent the primary keyword naturally in the opening paragraph", prompt)
        self.assertIn("You may adjust word order, add small connecting words", prompt)
        self.assertNotIn("must appear naturally in the first sentence", prompt)
        self.assertNotIn("Primary keyword in the first sentence", prompt)

    def test_prompt_allows_up_to_three_supporting_keywords_when_natural(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=[
                "technical SEO audit",
                "ecommerce SEO audit",
                "site audit services",
                "SEO health check",
            ],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="",
            forbidden_phrases="",
            brand_profile={},
        )

        self.assertIn("weave up to 3 of these naturally into the copy where they fit", prompt)
        self.assertIn("A keyword used awkwardly is worse than not using it at all", prompt)
        self.assertIn("Quality of integration matters more than quantity", prompt)
        self.assertNotIn("weave 1-2 of these naturally", prompt)

    def test_prompt_uses_ai_overview_as_framing_signal_only(self):
        prompt = _build_prompt(
            primary_keyword="SEO audit services",
            supporting_keywords=[],
            page_template="service_lp",
            business_type="service",
            brand_name="Example",
            include_brand=False,
            h1="SEO Audit Services",
            word_count=80,
            paragraph_count=1,
            page_context="The page describes technical SEO audits for ecommerce teams.",
            forbidden_phrases="",
            brand_profile={},
            ai_overview_summary="Search results emphasize crawlability, indexation, and prioritizing revenue-impacting fixes.",
        )

        self.assertIn("AI OVERVIEW FRAMING SIGNAL", prompt)
        self.assertIn("crawlability, indexation", prompt)
        self.assertIn("Do not copy, quote, or treat this as proof", prompt)


class IntroSerpContextTests(unittest.TestCase):
    def _settings(self, include_ai_overview_context=True):
        return {
            "provider": "Claude",
            "api_key": "api-key",
            "dfs_login": "dfs-login",
            "dfs_password": "dfs-password",
            "location_code": 2840,
            "min_volume": 10,
            "scrape_pages": False,
            "include_ai_overview_context": include_ai_overview_context,
            "page_template": "service_lp",
            "business_type": "service",
            "brand_name": "Example",
            "include_brand": False,
            "word_count": 80,
            "paragraph_count": 1,
        }

    def _selection(self):
        return {
            "primary": {"keyword": "SEO audit services", "volume": 100, "difficulty": 30},
            "supporting": [{"keyword": "technical SEO audit"}],
            "runner_up": None,
            "cluster_source": "manual",
        }

    def _valid_intro(self):
        return " ".join(["word"] * 80)

    def test_enabled_ai_overview_summary_is_passed_to_generator(self):
        with patch.object(intro, "get_ranked_keywords_for_page", return_value=[]), \
             patch.object(intro, "get_keyword_overview", return_value={}), \
             patch.object(intro, "get_keyword_difficulty", return_value={}), \
             patch.object(intro, "select_intro_keywords", return_value=self._selection()), \
             patch.object(intro, "get_ai_overview_summary", return_value="AIO summary for buyer intent.") as mock_aio, \
             patch.object(intro, "generate_intro", return_value=self._valid_intro()) as mock_generate:

            result = intro._process_single_row(
                row={"url": "https://example.com/services/seo-audit", "keyword": "SEO audit services", "h1": "SEO Audit Services"},
                settings=self._settings(include_ai_overview_context=True),
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["status"], "ok")
        mock_aio.assert_called_once()
        self.assertEqual(mock_generate.call_args.kwargs["ai_overview_summary"], "AIO summary for buyer intent.")

    def test_ai_overview_failure_does_not_block_generation(self):
        with patch.object(intro, "get_ranked_keywords_for_page", return_value=[]), \
             patch.object(intro, "get_keyword_overview", return_value={}), \
             patch.object(intro, "get_keyword_difficulty", return_value={}), \
             patch.object(intro, "select_intro_keywords", return_value=self._selection()), \
             patch.object(intro, "get_ai_overview_summary", side_effect=RuntimeError("timeout")), \
             patch.object(intro, "generate_intro", return_value=self._valid_intro()) as mock_generate:

            result = intro._process_single_row(
                row={"url": "https://example.com/services/seo-audit", "keyword": "SEO audit services", "h1": "SEO Audit Services"},
                settings=self._settings(include_ai_overview_context=True),
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(mock_generate.call_args.kwargs["ai_overview_summary"], "")

    def test_disabled_ai_overview_context_skips_serp_call(self):
        with patch.object(intro, "get_ranked_keywords_for_page", return_value=[]), \
             patch.object(intro, "get_keyword_overview", return_value={}), \
             patch.object(intro, "get_keyword_difficulty", return_value={}), \
             patch.object(intro, "select_intro_keywords", return_value=self._selection()), \
             patch.object(intro, "get_ai_overview_summary") as mock_aio, \
             patch.object(intro, "generate_intro", return_value=self._valid_intro()) as mock_generate:

            result = intro._process_single_row(
                row={"url": "https://example.com/services/seo-audit", "keyword": "SEO audit services", "h1": "SEO Audit Services"},
                settings=self._settings(include_ai_overview_context=False),
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["status"], "ok")
        mock_aio.assert_not_called()
        self.assertEqual(mock_generate.call_args.kwargs["ai_overview_summary"], "")

    def test_short_intro_is_flagged_for_review_without_dropping_copy(self):
        short_intro = "Short intro copy with too few words for the requested target."
        with patch.object(intro, "get_ranked_keywords_for_page", return_value=[]), \
             patch.object(intro, "get_keyword_overview", return_value={}), \
             patch.object(intro, "get_keyword_difficulty", return_value={}), \
             patch.object(intro, "select_intro_keywords", return_value=self._selection()), \
             patch.object(intro, "get_ai_overview_summary", return_value=""), \
             patch.object(intro, "generate_intro", return_value=short_intro):

            result = intro._process_single_row(
                row={"url": "https://example.com/services/seo-audit", "keyword": "SEO audit services", "h1": "SEO Audit Services"},
                settings={**self._settings(include_ai_overview_context=False), "word_count": 100},
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["intro_copy"], short_intro)
        self.assertEqual(result["status"], "review")
        self.assertIsNone(result["error"])
        self.assertIn("Intro is very short.", result["qa_flags"])

    def test_paragraph_count_mismatch_is_flagged_for_review(self):
        one_paragraph = " ".join(["word"] * 95)
        with patch.object(intro, "get_ranked_keywords_for_page", return_value=[]), \
             patch.object(intro, "get_keyword_overview", return_value={}), \
             patch.object(intro, "get_keyword_difficulty", return_value={}), \
             patch.object(intro, "select_intro_keywords", return_value=self._selection()), \
             patch.object(intro, "get_ai_overview_summary", return_value=""), \
             patch.object(intro, "generate_intro", return_value=one_paragraph):

            result = intro._process_single_row(
                row={"url": "https://example.com/services/seo-audit", "keyword": "SEO audit services", "h1": "SEO Audit Services"},
                settings={**self._settings(include_ai_overview_context=False), "word_count": 100, "paragraph_count": 2},
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["status"], "review")
        self.assertIsNone(result["error"])
        self.assertIn("Paragraph count mismatch: expected 2, got 1.", result["qa_flags"])

    def test_generic_intro_opener_is_flagged_for_review(self):
        generic_intro = "Looking for SEO audit services that make sense for your business? " + " ".join(["word"] * 70)
        with patch.object(intro, "get_ranked_keywords_for_page", return_value=[]), \
             patch.object(intro, "get_keyword_overview", return_value={}), \
             patch.object(intro, "get_keyword_difficulty", return_value={}), \
             patch.object(intro, "select_intro_keywords", return_value=self._selection()), \
             patch.object(intro, "get_ai_overview_summary", return_value=""), \
             patch.object(intro, "generate_intro", return_value=generic_intro):

            result = intro._process_single_row(
                row={"url": "https://example.com/services/seo-audit", "keyword": "SEO audit services", "h1": "SEO Audit Services"},
                settings={**self._settings(include_ai_overview_context=False), "word_count": 80},
                gsc_client=None,
                branded_terms=[],
                used_primaries=set(),
                user_id="user-1",
            )

        self.assertEqual(result["status"], "review")
        self.assertIn('Generic opener found: "Looking for".', result["qa_flags"])


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
