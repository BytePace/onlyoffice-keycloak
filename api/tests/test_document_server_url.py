import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


class DocumentServerApiBaseTests(unittest.TestCase):
    def test_prefers_public_api_url_for_document_server(self):
        with patch.object(main, "API_EXTERNAL_URL", "https://sheets.example.com/api"), patch.object(
            main, "API_INTERNAL_URL", "http://api:8000"
        ):
            self.assertEqual(main._document_server_api_base(), "https://sheets.example.com/api")

    def test_falls_back_to_internal_when_public_missing(self):
        with patch.object(main, "API_EXTERNAL_URL", ""), patch.object(
            main, "API_INTERNAL_URL", "http://api:8000"
        ):
            self.assertEqual(main._document_server_api_base(), "http://api:8000")

    def test_build_editor_config_uses_public_file_url(self):
        meta = {"title": "Sheet", "name": "Sheet"}
        user = {"email": "user@example.com"}
        with patch.object(main, "API_EXTERNAL_URL", "https://sheets.example.com/api"), patch.object(
            main, "API_INTERNAL_URL", "http://api:8000"
        ):
            cfg = main._build_editor_config(meta, user, "doc-1", "sess-1", 2)
        self.assertTrue(
            cfg["document"]["url"].startswith("https://sheets.example.com/api/docs/doc-1/file.xlsx")
        )
        self.assertIn("editor_session=sess-1", cfg["document"]["url"])


if __name__ == "__main__":
    unittest.main()
