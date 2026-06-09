import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


class EditorUrlTests(unittest.TestCase):
    def test_editor_url_uses_api_external_base(self):
        with patch.object(main, "API_EXTERNAL_URL", "https://sheets.example.com/api"):
            self.assertEqual(
                main._editor_url("abc-123"),
                "https://sheets.example.com/api/docs/abc-123/editor",
            )

    def test_editor_url_relative_when_base_missing(self):
        with patch.object(main, "API_EXTERNAL_URL", ""):
            self.assertEqual(main._editor_url("abc-123"), "/docs/abc-123/editor")


if __name__ == "__main__":
    unittest.main()
