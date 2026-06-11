import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mailer  # noqa: E402


class MailerTests(unittest.TestCase):
    def test_is_deliverable_email_accepts_real_mailbox(self):
        self.assertTrue(mailer.is_deliverable_email("owner@example.com"))

    def test_is_deliverable_email_rejects_nextcloud_uid_hash(self):
        self.assertFalse(
            mailer.is_deliverable_email(
                "3aa2e5d-f71c50bc969206a790f6eddf1d8557bd80b9a598b199c91713a85db2a"
            )
        )


if __name__ == "__main__":
    unittest.main()
