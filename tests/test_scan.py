"""Scans must not lie.

A catalog is only useful if "scanned today, N files" means it. These pin the
two ways a scan used to overstate itself: folders it could not read, and a
drive yanked out halfway through.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import os
import shutil
import sqlite3
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_catalog():
    spec = importlib.util.spec_from_file_location(
        "catalog_under_test", os.path.join(_ROOT, "catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


catalog = _load_catalog()


class ScanTruthfulness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hddcat-test-")
        self.db = os.path.join(self.tmp, "t.db")
        self.drive = os.path.join(self.tmp, "DriveA")
        os.makedirs(os.path.join(self.drive, "shoot"))
        for i in range(3):
            with open(os.path.join(self.drive, "shoot", "f%d.arw" % i), "w") as f:
                f.write("x")

    def tearDown(self):
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _files_in_catalog(self, label):
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM files WHERE drive_label=?",
                         (label,)).fetchone()[0]
        conn.close()
        return n

    def test_clean_scan_reports_no_unreadable(self):
        res = catalog.scan_drive(self.db, self.drive, "A")
        self.assertEqual(res["files"], 3)
        self.assertEqual(res["unreadable"], 0)

    def test_unreadable_folder_is_counted_not_swallowed(self):
        locked = os.path.join(self.drive, "locked")
        os.makedirs(locked)
        with open(os.path.join(locked, "hidden.arw"), "w") as f:
            f.write("x")
        os.chmod(locked, 0o000)
        if os.access(locked, os.R_OK):  # running as root: the premise is gone
            self.skipTest("cannot make a folder unreadable as this user")
        res = catalog.scan_drive(self.db, self.drive, "A")
        self.assertEqual(res["files"], 3, "the readable files are still cataloged")
        self.assertGreaterEqual(res["unreadable"], 1, "the locked folder is reported")
        self.assertTrue(res["unreadable_sample"], "and named, so the user can fix it")

    def test_drive_vanishing_mid_scan_keeps_the_old_catalog(self):
        catalog.scan_drive(self.db, self.drive, "A")
        self.assertEqual(self._files_in_catalog("A"), 3)

        real_walk = catalog.os.walk

        def walk_then_yank(path, *a, **kw):
            # one folder's worth of results, then the drive is gone - exactly
            # what os.walk does when a USB disk is pulled out mid-scan
            for i, entry in enumerate(real_walk(path, *a, **kw)):
                yield entry
                if i == 0:
                    shutil.rmtree(self.drive, ignore_errors=True)
                    return

        catalog.os.walk = walk_then_yank
        try:
            with self.assertRaises(ValueError):
                catalog.scan_drive(self.db, self.drive, "A")
        finally:
            catalog.os.walk = real_walk
        self.assertEqual(self._files_in_catalog("A"), 3,
                         "an interrupted scan must not replace a good catalog")

    def test_unreadable_capacity_does_not_blank_what_we_knew(self):
        first = catalog.scan_drive(self.db, self.drive, "A")
        self.assertIsNotNone(first["disk_total"])

        real_usage = catalog.shutil.disk_usage

        def boom(_path):
            raise OSError("no statfs for you")

        catalog.shutil.disk_usage = boom
        try:
            res = catalog.scan_drive(self.db, self.drive, "A")
        finally:
            catalog.shutil.disk_usage = real_usage
        self.assertEqual(
            res["disk_total"], first["disk_total"],
            "capacity survives; NULL would hide the drive from the move planner")


if __name__ == "__main__":
    unittest.main()
