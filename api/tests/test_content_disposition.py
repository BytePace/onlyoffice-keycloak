import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import nextcloud  # noqa: E402


class ContentDispositionTests(unittest.TestCase):
    def test_cyrillic_filename_is_latin1_safe(self):
        header = nextcloud.content_disposition_inline("киндзадза.xlsx")
        header.encode("latin-1")
        self.assertIn("filename=\"", header)
        self.assertIn("filename*=UTF-8''", header)


if __name__ == "__main__":
    unittest.main()
