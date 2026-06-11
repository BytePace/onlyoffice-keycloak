import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import access_requests  # noqa: E402


class AccessRequestsStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._data_patch = patch.object(access_requests, "DATA_DIR", Path(self._tmpdir.name))
        self._data_patch.start()

    def tearDown(self):
        self._data_patch.stop()
        self._tmpdir.cleanup()

    def test_create_and_fetch_pending_request(self):
        record = access_requests.create_or_refresh_request(
            doc_id="doc-1",
            doc_title="Food Diary",
            requester_email="user2@example.com",
            owner_email="owner@example.com",
        )
        self.assertEqual(record["status"], "pending")
        loaded = access_requests.get_request(record["token"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["doc_title"], "Food Diary")

    def test_refresh_pending_request_reuses_token(self):
        first = access_requests.create_or_refresh_request(
            doc_id="doc-1",
            doc_title="Food Diary",
            requester_email="user2@example.com",
            owner_email="owner@example.com",
        )
        second = access_requests.create_or_refresh_request(
            doc_id="doc-1",
            doc_title="Food Diary v2",
            requester_email="user2@example.com",
            owner_email="owner@example.com",
        )
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(second["doc_title"], "Food Diary v2")

    def test_update_request_status(self):
        record = access_requests.create_or_refresh_request(
            doc_id="doc-1",
            doc_title="Food Diary",
            requester_email="user2@example.com",
            owner_email="owner@example.com",
        )
        updated = access_requests.update_request_status(
            record["token"],
            status="granted",
            resolved_by="owner@example.com",
        )
        self.assertEqual(updated["status"], "granted")
        self.assertEqual(updated["resolved_by"], "owner@example.com")


if __name__ == "__main__":
    unittest.main()
