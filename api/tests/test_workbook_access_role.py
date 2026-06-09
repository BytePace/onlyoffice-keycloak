import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import nextcloud  # noqa: E402


class WorkbookAccessRoleTests(unittest.TestCase):
    def test_ocs_file_share_uses_explicit_role(self):
        share = {"owner_id": "owner@example.com", "role": "editor"}
        self.assertEqual(
            nextcloud.workbook_access_role(share, "/SSA Forms (2)/book.xlsx"),
            "editor",
        )

    def test_webdav_in_shared_mount_inherits_folder_editor_role(self):
        share = {"owner_id": "", "role": "viewer"}
        roles = {"SSA Forms (2)": "editor"}
        mounts = {"SSA Forms (2)"}
        self.assertEqual(
            nextcloud.workbook_access_role(
                share,
                "/SSA Forms (2)/book.xlsx",
                folder_share_roles=roles,
                shared_mount_names=mounts,
            ),
            "editor",
        )

    def test_webdav_in_shared_mount_defaults_to_editor_without_folder_role(self):
        share = {"owner_id": "", "role": "viewer"}
        mounts = {"Team Folder"}
        self.assertEqual(
            nextcloud.workbook_access_role(
                share,
                "/Team Folder/report.xlsx",
                shared_mount_names=mounts,
            ),
            "editor",
        )

    def test_own_file_without_mount_stays_viewer(self):
        share = {"owner_id": "", "role": "viewer"}
        self.assertEqual(
            nextcloud.workbook_access_role(share, "/MySheet.xlsx"),
            "viewer",
        )


if __name__ == "__main__":
    unittest.main()
