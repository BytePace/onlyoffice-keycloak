import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage  # noqa: E402


class ContentRevisionTests(unittest.TestCase):
    def test_bump_content_revision_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(storage, "DATA_DIR", Path(tmp)):
                meta = storage.create_document("Book", "owner@example.com", "/book.xlsx")
                doc_id = meta["id"]
                self.assertEqual(storage.get_content_revision(meta), 0)
                self.assertEqual(storage.bump_content_revision(doc_id), 1)
                updated = storage.get_document_meta(doc_id)
                self.assertEqual(storage.get_content_revision(updated), 1)
                self.assertEqual(storage.bump_content_revision(doc_id), 2)


if __name__ == "__main__":
    unittest.main()
