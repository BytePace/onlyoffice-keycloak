import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app.main import http_exception_handler  # noqa: E402


class HTTPExceptionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_errors_return_json(self):
        request = MagicMock()
        request.url.path = "/api/orgs/1/workspaces"
        request.headers.get.return_value = "application/json"

        response = await http_exception_handler(
            request,
            HTTPException(status_code=403, detail="Access denied"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            json.loads(response.body),
            {"detail": "Access denied"},
        )

    async def test_non_api_errors_return_html(self):
        request = MagicMock()
        request.url.path = "/login"
        request.headers.get.return_value = "text/html"

        response = await http_exception_handler(
            request,
            HTTPException(status_code=403, detail="Access denied"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"<h1>403</h1>", response.body)
        self.assertIn(b"<p>Access denied</p>", response.body)


if __name__ == "__main__":
    unittest.main()
