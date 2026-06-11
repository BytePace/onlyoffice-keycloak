import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import access_requests, main  # noqa: E402


class AccessRequestRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._data_patch = patch.object(access_requests, "DATA_DIR", Path(self._tmpdir.name))
        self._data_patch.start()

    def tearDown(self):
        self._data_patch.stop()
        self._tmpdir.cleanup()

    async def test_review_page_shows_login_when_anonymous(self):
        record = access_requests.create_or_refresh_request(
            doc_id="doc-1",
            doc_title="Food Diary",
            requester_email="user2@example.com",
            owner_email="owner@example.com",
        )
        request = type("Req", (), {"cookies": {}, "headers": {}})()

        with patch.object(main, "_optional_current_user", new=AsyncMock(return_value=None)):
            response = await main.access_request_review_page(record["token"], request)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sign in to respond", response.body)

    async def test_grant_marks_request_granted(self):
        record = access_requests.create_or_refresh_request(
            doc_id="doc-1",
            doc_title="Food Diary",
            requester_email="user2@example.com",
            owner_email="owner@example.com",
        )
        owner = {"email": "owner@example.com"}
        meta = {
            "id": "doc-1",
            "title": "Food Diary",
            "owner_email": "owner@example.com",
            "shared_with": {},
            "nextcloud_path": "/Food Diary.xlsx",
        }
        request = type("Req", (), {"cookies": {}, "headers": {}})()

        with patch.object(
            main,
            "_resolve_access_request_as_owner",
            new=AsyncMock(return_value=(record, meta, "owner-token")),
        ), patch.object(
            main,
            "_grant_document_access",
            new=AsyncMock(return_value={"meta": meta}),
        ):
            response = await main.grant_access_request(record["token"], request, owner)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Access granted", response.body)
        updated = access_requests.get_request(record["token"])
        self.assertEqual(updated["status"], "granted")


if __name__ == "__main__":
    unittest.main()
