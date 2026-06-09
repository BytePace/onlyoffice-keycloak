import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import onlyoffice  # noqa: E402


class DocumentKeyTests(unittest.TestCase):
    def test_document_key_changes_with_revision(self):
        k1 = onlyoffice.document_key("doc-1", 1)
        k2 = onlyoffice.document_key("doc-1", 2)
        self.assertNotEqual(k1, k2)

    def test_build_editor_config_uses_revision_key(self):
        cfg = onlyoffice.build_editor_config(
            doc_id="doc-1",
            title="Sheet",
            user_email="user@example.com",
            file_url="http://nc-api:8000/docs/doc-1/file.xlsx",
            callback_url="http://nc-api:8000/docs/doc-1/callback",
            revision=7,
        )
        self.assertEqual(cfg["document"]["key"], onlyoffice.document_key("doc-1", 7))


class EditorEmbedPageTests(unittest.TestCase):
    def test_embed_page_uses_refresh_file_not_iframe_reload(self):
        html = onlyoffice.render_editor_embed_page(
            doc_id="doc-1",
            editor_session="sess-abc",
            config={"document": {"key": "abc"}},
            revision=3,
            poll_ms=2000,
        )
        self.assertIn("refreshFile", html)
        self.assertIn("editor-config", html)
        self.assertNotIn("<iframe", html)


if __name__ == "__main__":
    unittest.main()
