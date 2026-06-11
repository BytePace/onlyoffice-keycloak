import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app import access_requests, main  # noqa: E402


class RequestAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._data_patch = patch.object(access_requests, "DATA_DIR", Path(self._tmpdir.name))
        self._data_patch.start()

    def tearDown(self):
        self._data_patch.stop()
        self._tmpdir.cleanup()

    async def test_request_access_returns_granted_when_user_can_write(self):
        request = MagicMock()
        user = {"email": "user2@example.com"}
        meta = {
            "id": "doc-1",
            "title": "Food Diary",
            "owner_email": "owner@example.com",
            "shared_with": {},
        }

        with patch.object(main.storage, "get_document_meta", return_value=meta), patch.object(
            main, "_request_access_token", return_value="token"
        ), patch.object(
            main, "_user_with_nextcloud_id", new=AsyncMock(return_value=user)
        ), patch.object(
            main.nextcloud, "accept_all_pending_shares", new=AsyncMock(return_value=(0, []))
        ), patch.object(main.storage, "can_read", return_value=True), patch.object(
            main.storage, "can_write", return_value=True
        ), patch.object(main.storage, "get_doc_role", return_value="editor"):
            response = await main.request_doc_access("doc-1", request, user)

        self.assertEqual(response["status"], "granted")
        self.assertTrue(response["can_write"])
        self.assertFalse(response["email_sent"])

    async def test_request_access_saved_when_owner_email_cannot_be_resolved(self):
        request = MagicMock()
        user = {"email": "user2@example.com"}
        owner_nc_id = "3aa2e5d-f71c50bc969206a790f6eddf1d8557bd80b9a598b199c91713a85db2a"
        meta = {
            "id": "doc-1",
            "title": "Food Diary",
            "owner_email": owner_nc_id,
            "shared_with": {},
        }

        with patch.object(main.storage, "get_document_meta", return_value=meta), patch.object(
            main, "_request_access_token", return_value="token"
        ), patch.object(
            main, "_user_with_nextcloud_id", new=AsyncMock(return_value=user)
        ), patch.object(
            main.nextcloud, "accept_all_pending_shares", new=AsyncMock(return_value=(0, []))
        ), patch.object(
            main.nextcloud,
            "resolve_document_owner_notification",
            new=AsyncMock(return_value=(None, owner_nc_id)),
        ), patch.object(main.storage, "can_read", return_value=False), patch.object(
            main.storage, "can_write", return_value=False
        ), patch.object(main.storage, "get_doc_role", return_value=None), patch.object(
            main, "API_EXTERNAL_URL", "https://sheets.example.com/api"
        ):
            response = await main.request_doc_access("doc-1", request, user)

        self.assertEqual(response["status"], "request_saved")
        self.assertIsNone(response["owner_email"])
        self.assertFalse(response["email_sent"])
        self.assertIsNotNone(response["review_url"])
        self.assertNotIn("smtp", (response["email_error"] or "").lower())

    async def test_request_access_emails_owner_when_edit_access_missing(self):
        request = MagicMock()
        user = {"email": "user2@example.com"}
        meta = {
            "id": "doc-1",
            "title": "Food Diary",
            "owner_email": "owner@example.com",
            "shared_with": {},
        }

        with patch.object(main.storage, "get_document_meta", return_value=meta), patch.object(
            main, "_request_access_token", return_value="token"
        ), patch.object(
            main, "_user_with_nextcloud_id", new=AsyncMock(return_value=user)
        ), patch.object(
            main.nextcloud, "accept_all_pending_shares", new=AsyncMock(return_value=(0, []))
        ), patch.object(
            main.nextcloud,
            "resolve_document_owner_notification",
            new=AsyncMock(return_value=("owner@example.com", None)),
        ), patch.object(main.storage, "can_read", return_value=False), patch.object(
            main.storage, "can_write", return_value=False
        ), patch.object(main.storage, "get_doc_role", return_value=None), patch.object(
            main.mailer, "smtp_configured", return_value=True
        ), patch.object(
            main.mailer,
            "send_access_request_email",
            side_effect=lambda **kwargs: None,
        ), patch.object(main, "API_EXTERNAL_URL", "https://sheets.example.com/api"):
            response = await main.request_doc_access("doc-1", request, user)

        self.assertEqual(response["status"], "request_sent")
        self.assertTrue(response["email_sent"])
        self.assertIsNotNone(response["review_url"])
        self.assertIn("/access-requests/", response["review_url"])

    async def test_request_access_document_not_found(self):
        request = MagicMock()
        user = {"email": "user2@example.com"}

        with patch.object(main.storage, "get_document_meta", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await main.request_doc_access("missing", request, user)

        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
