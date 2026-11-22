import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from src.web_reader import WebReader, sanitize_url


class TestWebReaderCache(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "web_cache.db")
        self.orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp_dir.name
        WebReader.clear_memory_cache()

    def tearDown(self):
        WebReader.clear_memory_cache()
        if self.orig_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.orig_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        self.tmp_dir.cleanup()

    def test_sanitize_url(self):
        # Sensitive query params must be stripped
        url1 = "https://api.example.com/docs?token=secret123&page=1&auth=xyz"
        clean1 = sanitize_url(url1)
        self.assertNotIn("secret123", clean1)
        self.assertNotIn("xyz", clean1)
        self.assertIn("page=1", clean1)

        # S3 presigned params stripped
        url2 = "https://s3.amazonaws.com/bucket/doc.pdf?X-Amz-Signature=abc&X-Amz-Credential=def&v=2"
        clean2 = sanitize_url(url2)
        self.assertNotIn("X-Amz-Signature", clean2)
        self.assertNotIn("X-Amz-Credential", clean2)
        self.assertIn("v=2", clean2)

        # Non-sensitive query params preserved
        url3 = "https://docs.python.org/3/library/unittest.html?highlight=mock"
        clean3 = sanitize_url(url3)
        self.assertEqual(url3, clean3)

    @patch("urllib.request.build_opener")
    def test_l1_l2_caching_and_tier_tracking(self, mock_build_opener):
        # Mock opener and network response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html><body><h1>Test Docs</h1><p>Documentation content</p></body></html>"
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.headers = {"Content-Type": "text/html"}

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        url = "https://example.com/docs/api"

        # Turn 1: Network fetch (tier = live)
        content1, ok1, _ = WebReader.fetch_url(url)
        self.assertTrue(ok1)
        self.assertIn("Test Docs", content1)
        self.assertEqual(WebReader.last_tier, "live")
        self.assertEqual(mock_opener.open.call_count, 1)

        # Turn 2: L1 Memory hit (tier = L1)
        content2, ok2, _ = WebReader.fetch_url(url)
        self.assertTrue(ok2)
        self.assertEqual(WebReader.last_tier, "L1")
        self.assertEqual(mock_opener.open.call_count, 1)

        # Clear L1 to test L2 SQLite hit
        WebReader.clear_memory_cache()
        content3, ok3, _ = WebReader.fetch_url(url)
        self.assertTrue(ok3)
        self.assertEqual(WebReader.last_tier, "L2")
        self.assertEqual(mock_opener.open.call_count, 1)

        # Turn 4: Force refresh bypasses cache
        content4, ok4, _ = WebReader.fetch_url(url, force_refresh=True)
        self.assertTrue(ok4)
        self.assertEqual(WebReader.last_tier, "live")
        self.assertEqual(mock_opener.open.call_count, 2)

    @patch("src.team_cache.TeamCache.get")
    @patch("src.team_cache.TeamCache.put")
    @patch("urllib.request.build_opener")
    def test_l3_team_cache_hit_and_put(self, mock_build_opener, mock_team_put, mock_team_get):
        url = "https://example.com/shared/team-doc"
        # Simulate L3 cache hit
        mock_team_get.return_value = "# Shared Team Documentation\nContent from L3"

        content, ok, _ = WebReader.fetch_url(url)
        self.assertTrue(ok)
        self.assertEqual(WebReader.last_tier, "L3")
        self.assertIn("Content from L3", content)
        mock_build_opener.assert_not_called()

    @patch("src.team_cache.TeamCache.put")
    @patch("urllib.request.build_opener")
    def test_authenticated_requests_bypass_l3(self, mock_build_opener, mock_team_put):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html><body><h1>Private Docs</h1></body></html>"
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.headers = {"Content-Type": "text/html"}

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        url = "https://internal.example.com/docs"
        headers = {"Authorization": "Bearer private-token-123"}

        content, ok, _ = WebReader.fetch_url(url, custom_headers=headers)
        self.assertTrue(ok)
        self.assertEqual(WebReader.last_tier, "live")
        # Should NOT push private docs with Auth headers to L3 TeamCache
        mock_team_put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
