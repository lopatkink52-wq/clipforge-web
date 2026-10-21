"""Small smoke tests for deterministic helpers; no model download required."""
import unittest
from core import is_http_url, ass_time, score_candidate

class CoreSmokeTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertTrue(is_http_url("https://example.com/watch?v=123"))
        self.assertFalse(is_http_url("file:///etc/passwd"))
        self.assertFalse(is_http_url("http://127.0.0.1/admin"))
        self.assertFalse(is_http_url("http://localhost:8000"))
        self.assertFalse(is_http_url("https://user:pass@example.com/"))

    def test_ass_time(self):
        self.assertEqual(ass_time(0), "0:00:00.00")
        self.assertEqual(ass_time(65.239), "0:01:05.23")

    def test_score_is_bounded(self):
        for text in ("", "Why did this happen? Because the answer was unexpected."):
            score = score_candidate(text, 0.7, 30)
            self.assertGreaterEqual(score, 1)
            self.assertLessEqual(score, 100)

if __name__ == "__main__":
    unittest.main()
