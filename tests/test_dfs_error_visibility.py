import unittest
from unittest.mock import Mock, patch

import requests

from utils import dfs


class DataForSeoErrorVisibilityTests(unittest.TestCase):
    @patch("utils.dfs.requests.post", side_effect=requests.Timeout("request timed out"))
    def test_keyword_helpers_raise_contextual_network_errors(self, _post):
        for helper in (dfs.get_keyword_overview, dfs.get_keyword_difficulty):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(RuntimeError, "DataForSEO request timed out"):
                    helper("login", "password", ["widgets"])

    @patch("utils.dfs.requests.post")
    def test_keyword_helper_raises_dataforseo_api_error(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status_code": 20000,
            "status_message": "Ok.",
            "tasks": [{
                "status_code": 40100,
                "status_message": "Authentication failed",
                "result": None,
            }],
        }
        post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "Invalid DataForSEO login or password"):
            dfs.get_keyword_overview("login", "password", ["widgets"])

if __name__ == "__main__":
    unittest.main()
